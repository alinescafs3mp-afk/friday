#!/usr/bin/env python3
"""Authenticate and publish one body-free production read-only observation.

The live backend owns the SQLite connection and produces the narrow snapshot.
This controller owns freshness, the sealed-release/process/TLS checks around
that snapshot, and create-only publication.  The endpoint response alone is
never evidence authority.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import ssl
import stat
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

if TYPE_CHECKING:
    from tools.immutable_release_operator import ReleaseIdentity as ReleaseIdentityT
    from tools.immutable_release_operator import SystemdConfig as SystemdConfigT
else:
    ReleaseIdentityT = Any
    SystemdConfigT = Any


def _load_sibling_release_operator() -> Any:
    source = Path(__file__).resolve(strict=True).with_name("immutable_release_operator.py")
    status = source.stat()
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid() or status.st_nlink != 1:
        raise ImportError("release_operator_origin_invalid")
    name = "_friday_production_observation_release_operator"
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(str(getattr(existing, "__file__", ""))).resolve(strict=True) != source:
            raise ImportError("release_operator_origin_invalid")
        return existing
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError("release_operator_origin_invalid")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    if Path(str(getattr(module, "__file__", ""))).resolve(strict=True) != source:
        sys.modules.pop(name, None)
        raise ImportError("release_operator_origin_invalid")
    return module


release_operator = _load_sibling_release_operator()

ARTIFACT_SCHEMA = "friday.production-read-only-release-captain-artifact.v1"
OBSERVATION_SCHEMA = "friday.production-read-only-observation.v1"
OBSERVATION_URL = "https://127.0.0.1:8000/api/admin/production-read-only-observation"
CHALLENGE_HEADER = "X-Friday-Production-Observation-Challenge-SHA256"
MAX_RESPONSE_BYTES = 65_536
MAX_OBSERVATION_BYTES = 32_768
MAX_ARTIFACT_BYTES = 65_536
MAX_COUNT = (1 << 63) - 1
PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 = (
    "726ded0b802ee1c6bf82663fd0918efb7f3d509f382c0d2aaa3540d4a1790561"
)

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CONCRETE_PATH_TYPE = type(Path())
_MISSION_STATES = (
    "blocked",
    "cancelled",
    "completed",
    "failed",
    "paused",
    "proposed",
    "ready",
    "running",
)
_TASK_STATES = (
    "compensated",
    "done",
    "failed",
    "pending",
    "running",
    "skipped",
    "uncertain",
)
_REMINDER_STATES = ("dismissed", "failed", "pending", "sent", "uncertain")
_WORKER_STATES = ("error", "ok", "running", "scheduled", "skipped", "timeout", "unknown")
_RELEASE_FIELDS = frozenset({"database_schema", "source_commit", "tree_sha256", "wheel_sha256"})
_ARTIFACT_FIELDS = frozenset(
    {
        "schema",
        "release",
        "production_observation_operator_sha256",
        "endpoint_response",
        "endpoint_response_sha256",
        "release_binding_sha256",
        "challenge_sha256",
        "backend_process_epoch_sha256",
        "health_before_sha256",
        "health_after_sha256",
    }
)


class ProductionObservationOperatorError(ValueError):
    """One closed Release Captain observation failure."""


class _ReleaseCaptainRuntime(Protocol):
    """Effect-free factors required by the controller."""

    def authenticate_release(self) -> Mapping[str, object]: ...

    def production_observation_operator_sha256(self) -> str: ...

    def process_epoch_sha256(self) -> str: ...

    def accepted_health_bytes(self) -> bytes: ...

    def observation_bytes(self, challenge_sha256: str) -> bytes: ...


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionObservationOperatorError("canonical_json_invalid") from exc


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionObservationOperatorError("canonical_json_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ProductionObservationOperatorError("canonical_json_invalid")


def _canonical_object(
    raw: bytes,
    *,
    code: str,
    maximum_bytes: int = MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ProductionObservationOperatorError(code)
    try:
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProductionObservationOperatorError(code) from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise ProductionObservationOperatorError(code)
    return value


def _digest(value: object, *, code: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None or set(value) == {"0"}:
        raise ProductionObservationOperatorError(code)
    return value


def _closed_counts(value: object, names: tuple[str, ...], *, code: str) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(names):
        raise ProductionObservationOperatorError(code)
    result: dict[str, int] = {}
    for name in names:
        count = value.get(name)
        if type(count) is not int or not 0 <= count <= MAX_COUNT:
            raise ProductionObservationOperatorError(code)
        result[name] = count
    return result


def validate_observation_response(
    raw: bytes,
    *,
    expected_challenge_sha256: str,
    expected_process_epoch_sha256: str,
) -> dict[str, Any]:
    """Validate the exact canonical, body-free backend response."""

    challenge = _digest(expected_challenge_sha256, code="challenge_invalid")
    epoch = _digest(expected_process_epoch_sha256, code="process_epoch_invalid")
    value = _canonical_object(
        raw,
        code="observation_response_invalid",
        maximum_bytes=MAX_OBSERVATION_BYTES,
    )
    if set(value) != {
        "schema",
        "challenge_sha256",
        "backend_process_epoch_sha256",
        "backend_lease_owned",
        "database",
        "scheduled_work",
        "hard_contradictions",
    }:
        raise ProductionObservationOperatorError("observation_response_invalid")
    if (
        value.get("schema") != OBSERVATION_SCHEMA
        or value.get("challenge_sha256") != challenge
        or value.get("backend_process_epoch_sha256") != epoch
        or value.get("backend_lease_owned") is not True
        or type(value.get("hard_contradictions")) is not int
        or value.get("hard_contradictions") != 0
    ):
        raise ProductionObservationOperatorError("observation_response_invalid")

    database = value.get("database")
    if (
        type(database) is not dict
        or set(database)
        != {"schema_version", "schema_attestation_sha256", "integrity", "foreign_key_violations"}
        or type(database.get("schema_version")) is not int
        or database.get("schema_version") != 50
        or database.get("integrity") != "ok"
        or type(database.get("foreign_key_violations")) is not int
        or database.get("foreign_key_violations") != 0
    ):
        raise ProductionObservationOperatorError("observation_database_invalid")
    if (
        _digest(database.get("schema_attestation_sha256"), code="observation_database_invalid")
        != PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256
    ):
        raise ProductionObservationOperatorError("observation_database_invalid")

    scheduled = value.get("scheduled_work")
    if type(scheduled) is not dict or set(scheduled) != {
        "missions",
        "mission_tasks",
        "reminders",
        "workers",
    }:
        raise ProductionObservationOperatorError("observation_scheduled_work_invalid")
    _closed_counts(scheduled.get("missions"), _MISSION_STATES, code="observation_counts_invalid")
    _closed_counts(scheduled.get("mission_tasks"), _TASK_STATES, code="observation_counts_invalid")
    _closed_counts(scheduled.get("reminders"), _REMINDER_STATES, code="observation_counts_invalid")
    workers = scheduled.get("workers")
    if type(workers) is not dict or set(workers) != {"present", "missing", "health_states"}:
        raise ProductionObservationOperatorError("observation_workers_invalid")
    present = workers.get("present")
    missing = workers.get("missing")
    health = _closed_counts(
        workers.get("health_states"),
        _WORKER_STATES,
        code="observation_workers_invalid",
    )
    if (
        type(present) is not int
        or type(missing) is not int
        or not 0 <= present <= 2
        or not 0 <= missing <= 2
        or present + missing != 2
        or sum(health.values()) != present
    ):
        raise ProductionObservationOperatorError("observation_workers_invalid")
    return value


def _release_payload(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _RELEASE_FIELDS:
        raise ProductionObservationOperatorError("release_identity_invalid")
    source_commit = value.get("source_commit")
    tree_sha256 = value.get("tree_sha256")
    wheel_sha256 = value.get("wheel_sha256")
    schema = value.get("database_schema")
    if (
        type(source_commit) is not str
        or _HEX40.fullmatch(source_commit) is None
        or type(tree_sha256) is not str
        or _HEX64.fullmatch(tree_sha256) is None
        or type(wheel_sha256) is not str
        or _HEX64.fullmatch(wheel_sha256) is None
        or type(schema) is not int
        or schema != 50
    ):
        raise ProductionObservationOperatorError("release_identity_invalid")
    return {
        "database_schema": schema,
        "source_commit": source_commit,
        "tree_sha256": tree_sha256,
        "wheel_sha256": wheel_sha256,
    }


def validate_release_captain_artifact(raw: bytes) -> dict[str, Any]:
    """Reject every artifact field which is not derived from its exact factors."""

    value = _canonical_object(raw, code="artifact_invalid")
    if set(value) != _ARTIFACT_FIELDS or value.get("schema") != ARTIFACT_SCHEMA:
        raise ProductionObservationOperatorError("artifact_invalid")
    release = _release_payload(value.get("release"))
    challenge = _digest(value.get("challenge_sha256"), code="artifact_invalid")
    epoch = _digest(value.get("backend_process_epoch_sha256"), code="artifact_invalid")
    for field_name in (
        "endpoint_response_sha256",
        "release_binding_sha256",
        "production_observation_operator_sha256",
        "health_before_sha256",
        "health_after_sha256",
    ):
        _digest(value.get(field_name), code="artifact_invalid")
    response = value.get("endpoint_response")
    if type(response) is not dict:
        raise ProductionObservationOperatorError("artifact_invalid")
    response_raw = canonical_json_bytes(response)
    validate_observation_response(
        response_raw,
        expected_challenge_sha256=challenge,
        expected_process_epoch_sha256=epoch,
    )
    if (
        value.get("endpoint_response_sha256") != hashlib.sha256(response_raw).hexdigest()
        or value.get("release_binding_sha256") != hashlib.sha256(canonical_json_bytes(release)).hexdigest()
    ):
        raise ProductionObservationOperatorError("artifact_invalid")
    return value


def _build_release_captain_artifact(
    runtime: _ReleaseCaptainRuntime,
    *,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> bytes:
    """Build one authenticated artifact without accepting caller claim fields."""

    if not callable(random_bytes):
        raise ProductionObservationOperatorError("challenge_source_invalid")
    release_before = _release_payload(runtime.authenticate_release())
    operator_before = _digest(
        runtime.production_observation_operator_sha256(),
        code="production_observation_operator_invalid",
    )
    epoch_before = _digest(runtime.process_epoch_sha256(), code="process_epoch_invalid")
    health_before = runtime.accepted_health_bytes()
    if type(health_before) is not bytes or not health_before or len(health_before) > MAX_RESPONSE_BYTES:
        raise ProductionObservationOperatorError("health_response_invalid")
    entropy = random_bytes(32)
    if type(entropy) is not bytes or len(entropy) != 32:
        raise ProductionObservationOperatorError("challenge_source_invalid")
    challenge_sha256 = hashlib.sha256(entropy).hexdigest()
    response_raw = runtime.observation_bytes(challenge_sha256)
    response = validate_observation_response(
        response_raw,
        expected_challenge_sha256=challenge_sha256,
        expected_process_epoch_sha256=epoch_before,
    )
    health_after = runtime.accepted_health_bytes()
    if type(health_after) is not bytes or not health_after or len(health_after) > MAX_RESPONSE_BYTES:
        raise ProductionObservationOperatorError("health_response_invalid")
    try:
        release_after = _release_payload(runtime.authenticate_release())
        operator_after = _digest(
            runtime.production_observation_operator_sha256(),
            code="production_observation_operator_invalid",
        )
        epoch_after = _digest(runtime.process_epoch_sha256(), code="process_epoch_invalid")
    except ProductionObservationOperatorError as exc:
        raise ProductionObservationOperatorError("observation_authority_drifted") from exc
    if epoch_after != epoch_before or release_after != release_before or operator_after != operator_before:
        raise ProductionObservationOperatorError("observation_authority_drifted")

    release_binding_sha256 = hashlib.sha256(canonical_json_bytes(release_before)).hexdigest()
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "release": release_before,
        "production_observation_operator_sha256": operator_before,
        "endpoint_response": response,
        "endpoint_response_sha256": hashlib.sha256(response_raw).hexdigest(),
        "release_binding_sha256": release_binding_sha256,
        "challenge_sha256": challenge_sha256,
        "backend_process_epoch_sha256": epoch_before,
        "health_before_sha256": hashlib.sha256(health_before).hexdigest(),
        "health_after_sha256": hashlib.sha256(health_after).hexdigest(),
    }
    raw = canonical_json_bytes(artifact)
    validate_release_captain_artifact(raw)
    return raw


def _write_artifact_create_only(path: Path, raw: bytes) -> str:
    """Publish exact canonical bytes once in an existing owner-private directory."""

    if type(path) is not _CONCRETE_PATH_TYPE or type(raw) is not bytes:
        raise ProductionObservationOperatorError("artifact_output_invalid")
    try:
        parent = release_operator._private_directory(path.parent)  # noqa: SLF001
        target = Path(os.path.abspath(path))
    except (OSError, release_operator.ReleaseFailure) as exc:
        raise ProductionObservationOperatorError("artifact_output_invalid") from exc
    if (
        not path.is_absolute()
        or target != path
        or target.parent != parent
        or target.name in {"", ".", ".."}
        or target.exists()
        or target.is_symlink()
    ):
        raise ProductionObservationOperatorError("artifact_output_invalid")
    validate_release_captain_artifact(raw)
    try:
        identity = release_operator._write_private_durable(  # noqa: SLF001
            target,
            raw,
            final_mode=0o400,
        )
        release_operator._fsync_directory(parent)  # noqa: SLF001
        status = os.stat(target, follow_symlinks=False)
        reread = release_operator._read_private_regular_file(  # noqa: SLF001
            target,
            maximum_bytes=MAX_ARTIFACT_BYTES,
            code="production_observation_artifact_invalid",
            allowed_modes=frozenset({0o400}),
        )
    except (OSError, release_operator.ReleaseFailure) as exc:
        raise ProductionObservationOperatorError("artifact_output_invalid") from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o400
        or identity != (int(status.st_dev), int(status.st_ino))
        or reread != raw
    ):
        raise ProductionObservationOperatorError("artifact_output_invalid")
    return hashlib.sha256(raw).hexdigest()


class SystemdReleaseCaptainRuntime:
    """Read-only adapter over the already active immutable release boundary."""

    def __init__(
        self,
        release: ReleaseIdentityT,
        config: SystemdConfigT,
    ) -> None:
        if (
            type(release) is not release_operator.ReleaseIdentity
            or type(config) is not release_operator.SystemdConfig
            or _HEX64.fullmatch(release.production_observation_operator_sha256) is None
        ):
            raise ProductionObservationOperatorError("runtime_boundary_invalid")
        if config.next_env_file is not None or config.next_env_file_sha256 or config.staged_config_transition:
            raise ProductionObservationOperatorError("staged_release_observation_forbidden")
        # Prevent SystemdActivationPort's compatibility create=True branch from
        # creating a missing backup directory during this read-only command.
        try:
            release_operator._private_directory(config.backup_dir)  # noqa: SLF001
            release_operator._require_runtime_operator_layout(config)  # noqa: SLF001
            self._port = release_operator.SystemdActivationPort(config)
        except (OSError, release_operator.ReleaseFailure) as exc:
            raise ProductionObservationOperatorError("runtime_boundary_invalid") from exc
        self.release = release
        self.config = config

    def _environment(self) -> bytes:
        try:
            raw = release_operator._read_private_regular_file(  # noqa: SLF001
                self.config.env_file,
                maximum_bytes=1 << 20,
                code="environment_file_invalid",
            )
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("environment_invalid") from exc
        if hashlib.sha256(raw).hexdigest() != self.config.env_file_sha256:
            raise ProductionObservationOperatorError("environment_invalid")
        return raw

    def _release_metadata(self) -> dict[str, object]:
        path = self.release.root / "artifacts/immutable-release.json"
        try:
            raw = release_operator._read_private_regular_file(  # noqa: SLF001
                path,
                maximum_bytes=1 << 20,
                code="release_metadata_invalid",
            )
            if not raw.endswith(b"\n"):
                raise ProductionObservationOperatorError("release_metadata_invalid")
            metadata = json.loads(
                raw[:-1].decode("ascii", errors="strict"),
                object_pairs_hook=_closed_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError, release_operator.ReleaseFailure) as exc:
            raise ProductionObservationOperatorError("release_metadata_invalid") from exc
        if type(metadata) is not dict or canonical_json_bytes(metadata) + b"\n" != raw:
            raise ProductionObservationOperatorError("release_metadata_invalid")
        wheel = metadata.get("wheel_sha256")
        if (
            metadata.get("commit") != self.release.commit
            or metadata.get("max_schema") != self.release.max_schema
            or type(wheel) is not str
            or _HEX64.fullmatch(wheel) is None
        ):
            raise ProductionObservationOperatorError("release_metadata_invalid")
        return {
            "database_schema": self.release.max_schema,
            "source_commit": self.release.commit,
            "tree_sha256": self.release.tree_manifest_sha256,
            "wheel_sha256": wheel,
        }

    def production_observation_operator_sha256(self) -> str:
        expected = _digest(
            self.release.production_observation_operator_sha256,
            code="production_observation_operator_invalid",
        )
        try:
            raw = release_operator._read_private_regular_file(  # noqa: SLF001
                Path(__file__),
                maximum_bytes=4 << 20,
                code="production_observation_operator_invalid",
                allowed_modes=frozenset({0o400}),
            )
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("production_observation_operator_invalid") from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ProductionObservationOperatorError("production_observation_operator_invalid")
        return expected

    def _require_active_unit(self) -> None:
        try:
            release_operator._require_release_in_operator_layout(  # noqa: SLF001
                self.release,
                self.config.friday_home,
            )
            self._port._verify_environment_file()  # noqa: SLF001
            if not self.config.anchor.is_symlink() or self.config.anchor.resolve(
                strict=True
            ) != self.release.root.resolve(strict=True):
                raise ProductionObservationOperatorError("active_anchor_invalid")
            unit = self.config.backend_unit
            if unit != "friday-backend.service":
                raise ProductionObservationOperatorError("active_unit_invalid")
            expected = self.release.root / "artifacts" / unit
            release_operator._verify_owned_static_file(  # noqa: SLF001
                self.config.unit_dir / unit,
                expected.read_bytes(),
                code="installed_unit_drift",
            )
            expected_dropins = release_operator._expected_unit_dropins(self.config, unit)  # noqa: SLF001
            dropin_directory = release_operator._owned_directory(  # noqa: SLF001
                self.config.unit_dir / f"{unit}.d"
            )
            if set(dropin_directory.iterdir()) != {path for path, _content in expected_dropins}:
                raise ProductionObservationOperatorError("active_unit_invalid")
            for path, content in expected_dropins:
                release_operator._verify_owned_static_file(  # noqa: SLF001
                    path,
                    content,
                    code="systemd_dropin_invalid",
                )

            expected_argv = (
                str(self.config.anchor / "venv/bin/python"),
                "-I",
                "-B",
                "-m",
                "friday.cli",
                "--env-file",
                str(self.config.env_file),
                "server",
            )
            manager_argv = release_operator._systemd_exec_argv(  # noqa: SLF001
                self._port._systemctl(  # noqa: SLF001
                    "show", unit, "--property=ExecStart", "--value"
                ).stdout,
                code="systemd_manager_execstart_invalid",
            )
            if manager_argv != expected_argv:
                raise ProductionObservationOperatorError("active_unit_invalid")
            pre_argv = release_operator._systemd_exec_argv(  # noqa: SLF001
                self._port._systemctl(  # noqa: SLF001
                    "show", unit, "--property=ExecStartPre", "--value"
                ).stdout,
                code="systemd_manager_execstartpre_invalid",
            )
            if pre_argv != ("/usr/bin/test", "-s", str(self.config.database)):
                raise ProductionObservationOperatorError("active_unit_invalid")
            for property_name in (
                "ExecCondition",
                "ExecStartPost",
                "ExecReload",
                "ExecStop",
                "ExecStopPost",
            ):
                if (
                    release_operator._systemd_exec_argv(  # noqa: SLF001
                        self._port._systemctl(  # noqa: SLF001
                            "show", unit, f"--property={property_name}", "--value"
                        ).stdout,
                        code="systemd_manager_extra_exec_invalid",
                    )
                    is not None
                ):
                    raise ProductionObservationOperatorError("active_unit_invalid")
            fragment = self._port._systemctl(  # noqa: SLF001
                "show", unit, "--property=FragmentPath", "--value"
            ).stdout
            fragment_path = release_operator._regular_file(  # noqa: SLF001
                Path(fragment.decode("utf-8", errors="strict").strip()),
                maximum_bytes=1 << 20,
                code="systemd_manager_fragment_invalid",
            )
            if fragment_path != self.config.unit_dir / unit:
                raise ProductionObservationOperatorError("active_unit_invalid")
            dropins = self._port._systemctl(  # noqa: SLF001
                "show", unit, "--property=DropInPaths", "--value"
            ).stdout
            manager_dropins = tuple(
                Path(value) for value in shlex.split(dropins.decode("utf-8", errors="strict"))
            )
            if manager_dropins != tuple(path for path, _content in expected_dropins):
                raise ProductionObservationOperatorError("active_unit_invalid")
            relevant = release_operator._systemd_environment(  # noqa: SLF001
                self._port._systemctl(  # noqa: SLF001
                    "show", unit, "--property=Environment", "--value"
                ).stdout,
                code="systemd_manager_environment_invalid",
            )
            if relevant != {
                "FRIDAY_DATABASE_MUST_EXIST": "1",
                "FRIDAY_DATABASE_PATH": str(self.config.database),
                "FRIDAY_HOME": str(self.config.friday_home),
                "TMPDIR": str(release_operator._unit_runtime_tmp_directory(unit)),  # noqa: SLF001
            }:
                raise ProductionObservationOperatorError("active_unit_invalid")
            exact_properties = {
                "KillMode": b"control-group",
                "UMask": b"0077",
                "UnitFileState": b"enabled",
                "EnvironmentFiles": b"",
                "LimitCORE": b"0",
                "PrivateTmp": b"no",
                "PrivateUsers": b"no",
                "RuntimeDirectory": release_operator._unit_runtime_directory_name(unit).encode(),  # noqa: SLF001
                "RuntimeDirectoryMode": b"0700",
                "RuntimeDirectoryPreserve": b"no",
                "UnsetEnvironment": b"PYTHONPATH",
                "WorkingDirectory": str(self.config.friday_home).encode(),
                "MemorySwapMax": b"0",
            }
            for property_name, expected_value in exact_properties.items():
                actual_value = self._port._systemctl(  # noqa: SLF001
                    "show", unit, f"--property={property_name}", "--value"
                ).stdout.strip()
                if actual_value != expected_value:
                    raise ProductionObservationOperatorError("active_unit_invalid")
            self._port._verify_backend_resource_limits()  # noqa: SLF001

            release_operator._verify_owned_static_file(  # noqa: SLF001
                self.config.unit_dir / unit,
                expected.read_bytes(),
                code="installed_unit_changed_during_attestation",
            )
            for path, content in expected_dropins:
                release_operator._verify_owned_static_file(  # noqa: SLF001
                    path,
                    content,
                    code="systemd_dropin_changed_during_attestation",
                )
            if not self.config.anchor.is_symlink() or self.config.anchor.resolve(
                strict=True
            ) != self.release.root.resolve(strict=True):
                raise ProductionObservationOperatorError("active_anchor_invalid")
        except (OSError, UnicodeError, ValueError, release_operator.ReleaseFailure) as exc:
            if isinstance(exc, ProductionObservationOperatorError):
                raise
            raise ProductionObservationOperatorError("active_unit_invalid") from exc

    def authenticate_release(self) -> Mapping[str, object]:
        try:
            self._port.verify_release(self.release)
            self._require_active_unit()
            self._port._current_backend_process_identity(self.release)  # noqa: SLF001
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("release_authentication_failed") from exc
        return self._release_metadata()

    def process_epoch_sha256(self) -> str:
        try:
            _pid, epoch = self._port._current_backend_process_identity(self.release)  # noqa: SLF001
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("process_epoch_invalid") from exc
        return epoch

    def _tls_opener(self) -> urllib.request.OpenerDirector:
        try:
            ca = release_operator._read_private_regular_file(  # noqa: SLF001
                self.config.health_ca,
                maximum_bytes=1 << 20,
                code="health_ca_invalid",
            )
            if hashlib.sha256(ca).hexdigest() != self.config.health_ca_sha256:
                raise ProductionObservationOperatorError("health_ca_invalid")
            context = ssl.create_default_context(cadata=ca.decode("ascii", errors="strict"))
        except (UnicodeError, ssl.SSLError, release_operator.ReleaseFailure) as exc:
            raise ProductionObservationOperatorError("health_ca_invalid") from exc
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            release_operator._NoRedirect(),  # noqa: SLF001
        )

    def _get(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._tls_opener().open(request, timeout=15.0) as response:
                status = int(response.status)
                response_url = response.geturl()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError, ssl.SSLError, urllib.error.URLError) as exc:
            raise ProductionObservationOperatorError("authenticated_request_failed") from exc
        if status != 200 or response_url != url or not raw or len(raw) > MAX_RESPONSE_BYTES:
            raise ProductionObservationOperatorError("authenticated_request_failed")
        return raw

    def accepted_health_bytes(self) -> bytes:
        try:
            self._port.accept_backend(self.release)
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("health_response_invalid") from exc
        raw = self._get(self.config.health_url, headers={"Accept": "application/json"})
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_closed_object,
                parse_constant=_reject_constant,
            )
            expected_semantic_mode = self._port._expected_semantic_health_mode()  # noqa: SLF001
            (
                expected_semantic_effect_mode,
                expected_semantic_effect_identity,
            ) = self._port._expected_semantic_effect_health()  # noqa: SLF001
            accepted = bool(
                type(value) is dict
                and value.get("status") == "ok"
                and value.get("version") == self.release.version
                and release_operator._memory_vault_health_identity_matches(  # noqa: SLF001
                    value,
                    self.release,
                    self.config.memory_vault_mode,
                )
                and release_operator._obsidian_health_identity_matches(  # noqa: SLF001
                    value,
                    self.release,
                    self.config.obsidian_mode,
                    release_operator._obsidian_root_sha256(self.config),  # noqa: SLF001
                )
                and (
                    not expected_semantic_mode
                    or release_operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
                        value,
                        expected_mode=expected_semantic_mode,
                    )
                )
                and (
                    not expected_semantic_effect_mode
                    or release_operator._semantic_effect_health_identity_matches(  # noqa: SLF001
                        value,
                        expected_mode=expected_semantic_effect_mode,
                        expected_identity=expected_semantic_effect_identity,
                    )
                )
            )
            if not accepted:
                raise ProductionObservationOperatorError("health_response_invalid")
            self._port._wait_process(  # noqa: SLF001
                self.config.backend_unit,
                self.release,
                "backend",
            )
        except (
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            release_operator.ReleaseFailure,
        ) as exc:
            if isinstance(exc, ProductionObservationOperatorError):
                raise
            raise ProductionObservationOperatorError("health_response_invalid") from exc
        return raw

    def observation_bytes(self, challenge_sha256: str) -> bytes:
        challenge = _digest(challenge_sha256, code="challenge_invalid")
        environment_before = self._environment()
        try:
            token = release_operator._secondary_rollout_api_token(environment_before)  # noqa: SLF001
        except release_operator.ReleaseFailure as exc:
            raise ProductionObservationOperatorError("owner_token_invalid") from exc
        raw = self._get(
            OBSERVATION_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                CHALLENGE_HEADER: challenge,
            },
        )
        if self._environment() != environment_before:
            raise ProductionObservationOperatorError("environment_changed")
        return raw


def publish_authenticated_release_captain_artifact(
    release: ReleaseIdentityT,
    config: SystemdConfigT,
    output: Path,
) -> dict[str, str]:
    """Observe and publish only through the concrete authenticated runtime.

    The injectable builder and raw create-only writer are deliberately private
    test seams.  The public boundary constructs its own systemd/release/TLS
    runtime, so a caller-owned object cannot replace authenticated methods.
    """

    if (
        type(release) is not release_operator.ReleaseIdentity
        or type(config) is not release_operator.SystemdConfig
    ):
        raise ProductionObservationOperatorError("runtime_boundary_invalid")
    runtime = SystemdReleaseCaptainRuntime(release, config)
    raw = _build_release_captain_artifact(runtime)
    artifact_sha256 = _write_artifact_create_only(output, raw)
    value = validate_release_captain_artifact(raw)
    return {
        "artifact_sha256": artifact_sha256,
        "endpoint_response_sha256": value["endpoint_response_sha256"],
        "release_binding_sha256": value["release_binding_sha256"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tree-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    release_operator._add_systemd_arguments(parser)  # noqa: SLF001
    return parser


def execute(args: argparse.Namespace) -> dict[str, object]:
    try:
        script = Path(__file__)
        lexical_script = Path(os.path.abspath(script))
        if (
            not script.is_absolute()
            or lexical_script != script
            or script.parent.name != "artifacts"
            or script.name != "production_read_only_observation_operator.py"
        ):
            raise ProductionObservationOperatorError("sealed_entrypoint_invalid")
        release_root = script.parent.parent
        release = release_operator.load_release_identity(
            release_root,
            expected_tree_sha256=args.release_tree_sha256,
        )
        expected_script = release.root / "artifacts/production_read_only_observation_operator.py"
        expected_operator = release.root / "artifacts/immutable_release_operator.py"
        running_operator = Path(str(getattr(release_operator, "__file__", "")))
        expected_python = release.root / "venv/bin/python"
        if (
            sys.flags.isolated != 1
            or sys.flags.dont_write_bytecode != 1
            or Path(os.path.abspath(sys.executable)) != expected_python
            or script.resolve(strict=True) != expected_script
            or running_operator.resolve(strict=True) != expected_operator
            or release.production_observation_operator_sha256
            != hashlib.sha256(
                release_operator._read_private_regular_file(  # noqa: SLF001
                    expected_script,
                    maximum_bytes=4 << 20,
                    code="production_observation_operator_invalid",
                    allowed_modes=frozenset({0o400}),
                )
            ).hexdigest()
        ):
            raise ProductionObservationOperatorError("sealed_entrypoint_invalid")
        for path in (expected_script, expected_operator):
            status = path.stat()
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_nlink != 1
                or stat.S_IMODE(status.st_mode) != 0o400
            ):
                raise ProductionObservationOperatorError("sealed_entrypoint_invalid")
        config = release_operator._systemd_config(args)  # noqa: SLF001
        published = publish_authenticated_release_captain_artifact(release, config, args.output)
    except release_operator.ReleaseFailure as exc:
        raise ProductionObservationOperatorError("release_authentication_failed") from exc
    return {
        "schema": ARTIFACT_SCHEMA,
        "status": "clear",
        **published,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        receipt = execute(build_parser().parse_args(argv))
        print(canonical_json_bytes(receipt).decode("ascii"))
        return 0
    except ProductionObservationOperatorError as exc:
        print(
            canonical_json_bytes(
                {
                    "schema": ARTIFACT_SCHEMA,
                    "status": "failed_closed",
                    "failure_code": str(exc),
                }
            ).decode("ascii")
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - never publish exception text
        print(
            canonical_json_bytes(
                {
                    "schema": ARTIFACT_SCHEMA,
                    "status": "failed_closed",
                    "failure_code": f"internal_{type(exc).__name__}",
                }
            ).decode("ascii")
        )
        return 3


__all__ = [
    "ARTIFACT_SCHEMA",
    "CHALLENGE_HEADER",
    "OBSERVATION_SCHEMA",
    "OBSERVATION_URL",
    "ProductionObservationOperatorError",
    "canonical_json_bytes",
    "publish_authenticated_release_captain_artifact",
    "validate_release_captain_artifact",
    "validate_observation_response",
]


if __name__ == "__main__":
    raise SystemExit(main())
