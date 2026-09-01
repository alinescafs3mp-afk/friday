"""Release-Captain binding for the hidden production observation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import production_read_only_observation_operator as operator

_RELEASE = {
    "database_schema": 50,
    "source_commit": "a" * 40,
    "tree_sha256": "b" * 64,
    "wheel_sha256": "c" * 64,
}
_EPOCH = "d" * 64
_OBSERVER_SHA256 = "e" * 64


def _zero(names: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(names, 0)


def _observation(challenge: str, *, epoch: str = _EPOCH) -> dict[str, object]:
    return {
        "schema": operator.OBSERVATION_SCHEMA,
        "challenge_sha256": challenge,
        "backend_process_epoch_sha256": epoch,
        "backend_lease_owned": True,
        "database": {
            "schema_version": 50,
            "schema_attestation_sha256": (operator.PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256),
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "scheduled_work": {
            "missions": {
                **_zero(
                    (
                        "proposed",
                        "ready",
                        "running",
                        "paused",
                        "blocked",
                        "completed",
                        "failed",
                        "cancelled",
                    )
                ),
                "ready": 2,
            },
            "mission_tasks": {
                **_zero(
                    (
                        "pending",
                        "running",
                        "done",
                        "failed",
                        "skipped",
                        "uncertain",
                        "compensated",
                    )
                ),
                "pending": 3,
            },
            "reminders": {
                **_zero(("pending", "uncertain", "sent", "failed", "dismissed")),
                "sent": 4,
                "dismissed": 1,
            },
            "workers": {
                "present": 2,
                "missing": 0,
                "health_states": {
                    **_zero(
                        (
                            "scheduled",
                            "running",
                            "ok",
                            "error",
                            "timeout",
                            "skipped",
                            "unknown",
                        )
                    ),
                    "ok": 2,
                },
            },
        },
        "hard_contradictions": 0,
    }


class _Runtime:
    def __init__(self) -> None:
        self.release_calls = 0
        self.epoch_calls = 0
        self.health_calls = 0
        self.observer_calls = 0
        self.challenge = ""
        self.release_after = dict(_RELEASE)
        self.epoch_after = _EPOCH
        self.observer_after = _OBSERVER_SHA256
        self.observation_mutator = lambda value: value

    def authenticate_release(self) -> dict[str, object]:
        self.release_calls += 1
        return dict(_RELEASE if self.release_calls == 1 else self.release_after)

    def process_epoch_sha256(self) -> str:
        self.epoch_calls += 1
        return _EPOCH if self.epoch_calls == 1 else self.epoch_after

    def production_observation_operator_sha256(self) -> str:
        self.observer_calls += 1
        return _OBSERVER_SHA256 if self.observer_calls == 1 else self.observer_after

    def accepted_health_bytes(self) -> bytes:
        self.health_calls += 1
        return f'{{"private":"PRIVATE-HEALTH-{self.health_calls}","status":"ok"}}'.encode()

    def observation_bytes(self, challenge_sha256: str) -> bytes:
        self.challenge = challenge_sha256
        value = self.observation_mutator(_observation(challenge_sha256))
        return operator.canonical_json_bytes(value)


def _artifact(runtime: _Runtime | None = None) -> tuple[bytes, _Runtime]:
    selected = runtime or _Runtime()
    raw = operator._build_release_captain_artifact(  # noqa: SLF001
        selected,
        random_bytes=lambda size: b"x" * size,
    )
    return raw, selected


def test_release_captain_derives_one_canonical_private_body_free_artifact() -> None:
    raw, runtime = _artifact()
    value = operator.validate_release_captain_artifact(raw)
    challenge = hashlib.sha256(b"x" * 32).hexdigest()

    assert runtime.challenge == challenge
    assert runtime.release_calls == runtime.epoch_calls == runtime.health_calls == runtime.observer_calls == 2
    assert value == json.loads(raw)
    assert value["challenge_sha256"] == challenge
    assert value["backend_process_epoch_sha256"] == _EPOCH
    assert value["release"] == _RELEASE
    assert value["production_observation_operator_sha256"] == _OBSERVER_SHA256
    assert value["endpoint_response"] == _observation(challenge)
    assert (
        value["endpoint_response_sha256"]
        == hashlib.sha256(operator.canonical_json_bytes(value["endpoint_response"])).hexdigest()
    )
    assert (
        value["release_binding_sha256"] == hashlib.sha256(operator.canonical_json_bytes(_RELEASE)).hexdigest()
    )
    assert value["health_before_sha256"] != value["health_after_sha256"]
    assert raw == operator.canonical_json_bytes(value)
    drifted_operator = _Runtime()
    drifted_operator.observer_after = "0" * 64
    with pytest.raises(
        operator.ProductionObservationOperatorError,
        match="observation_authority_drifted",
    ):
        _artifact(drifted_operator)

    rendered = raw.decode("ascii").casefold()
    for forbidden in (
        "private-health",
        '"pid"',
        '"path"',
        '"url"',
        '"timestamp"',
        '"body"',
        '"identity"',
        '"prompt"',
        '"output"',
        '"argv"',
        '"result"',
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("hard_contradictions", False),
        ("database.schema_version", 50.0),
        ("database.foreign_key_violations", False),
        ("scheduled_work.missions.ready", True),
        ("scheduled_work.workers.present", 2.0),
    ),
)
def test_observation_numeric_contract_rejects_json_aliases(field: str, value: object) -> None:
    challenge = "f" * 64
    payload = _observation(challenge)
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target[parts[-1]] = value

    with pytest.raises(operator.ProductionObservationOperatorError):
        operator.validate_observation_response(
            operator.canonical_json_bytes(payload),
            expected_challenge_sha256=challenge,
            expected_process_epoch_sha256=_EPOCH,
        )


@pytest.mark.parametrize(
    "raw",
    (
        b'{"schema":"friday.production-read-only-observation.v1","schema":"duplicate"}',
        b'{"schema":NaN}',
        b"{}\n",
        b" " * (operator.MAX_OBSERVATION_BYTES + 1),
        b"\xff",
    ),
)
def test_observation_serialization_is_exact_canonical_and_bounded(raw: bytes) -> None:
    with pytest.raises(operator.ProductionObservationOperatorError):
        operator.validate_observation_response(
            raw,
            expected_challenge_sha256="f" * 64,
            expected_process_epoch_sha256=_EPOCH,
        )


@pytest.mark.parametrize("drift", ("challenge", "epoch", "release"))
def test_release_captain_fails_closed_on_factor_drift(drift: str) -> None:
    runtime = _Runtime()
    if drift == "challenge":
        runtime.observation_mutator = lambda value: {**value, "challenge_sha256": "0" * 64}
    elif drift == "epoch":
        runtime.epoch_after = "0" * 64
    else:
        runtime.release_after["wheel_sha256"] = "0" * 64

    with pytest.raises(operator.ProductionObservationOperatorError):
        _artifact(runtime)


def test_restart_during_final_release_authentication_cannot_reuse_the_old_epoch() -> None:
    class RestartingRuntime(_Runtime):
        def authenticate_release(self) -> dict[str, object]:
            release = super().authenticate_release()
            if self.release_calls == 2:
                self.epoch_after = "0" * 64
            return release

    with pytest.raises(
        operator.ProductionObservationOperatorError,
        match="observation_authority_drifted",
    ):
        _artifact(RestartingRuntime())


@pytest.mark.parametrize("entropy", (b"", b"x" * 31, b"x" * 33))
def test_challenge_is_release_captain_issued_and_exact(entropy: bytes) -> None:
    with pytest.raises(operator.ProductionObservationOperatorError, match="challenge_source_invalid"):
        operator._build_release_captain_artifact(  # noqa: SLF001
            _Runtime(),
            random_bytes=lambda _size: entropy,
        )


def test_artifact_validator_recomputes_response_and_release_hashes() -> None:
    raw, _runtime = _artifact()
    value = json.loads(raw)

    for field in (
        "endpoint_response_sha256",
        "release_binding_sha256",
        "production_observation_operator_sha256",
    ):
        forged = {**value, field: "0" * 64}
        with pytest.raises(operator.ProductionObservationOperatorError, match="artifact_invalid"):
            operator.validate_release_captain_artifact(operator.canonical_json_bytes(forged))


def test_create_only_publication_is_owner_private_and_never_replaces(tmp_path: Path) -> None:
    raw, _runtime = _artifact()
    target = tmp_path / "production-observation.json"

    digest = operator._write_artifact_create_only(target, raw)  # noqa: SLF001

    status = target.stat()
    assert target.read_bytes() == raw
    assert digest == hashlib.sha256(raw).hexdigest()
    assert stat.S_IMODE(status.st_mode) == 0o400
    assert status.st_uid == os.geteuid()
    assert status.st_nlink == 1
    with pytest.raises(operator.ProductionObservationOperatorError, match="artifact_output_invalid"):
        operator._write_artifact_create_only(target, raw)  # noqa: SLF001


def test_create_only_publication_rejects_symlink_and_nonprivate_parent(tmp_path: Path) -> None:
    raw, _runtime = _artifact()
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"untouched")
    linked = tmp_path / "linked.json"
    linked.symlink_to(existing)

    with pytest.raises(operator.ProductionObservationOperatorError, match="artifact_output_invalid"):
        operator._write_artifact_create_only(linked, raw)  # noqa: SLF001
    assert existing.read_bytes() == b"untouched"

    exposed = tmp_path / "exposed"
    exposed.mkdir(mode=0o755)
    with pytest.raises(operator.ProductionObservationOperatorError, match="artifact_output_invalid"):
        operator._write_artifact_create_only(exposed / "artifact.json", raw)  # noqa: SLF001


def test_authoritative_publication_rejects_injectable_runtime_and_raw_writer(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.json"
    with pytest.raises(
        operator.ProductionObservationOperatorError,
        match="sealed_entrypoint_invalid",
    ):
        operator.execute(
            SimpleNamespace(
                release_tree_sha256="b" * 64,
                output=target,
            )
        )

    release_root = tmp_path / "sealed-release"
    artifacts = release_root / "artifacts"
    interpreter = release_root / "venv/bin/python"
    artifacts.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    shutil.copyfile(sys.executable, interpreter)
    interpreter.chmod(0o500)
    sealed_observer = artifacts / "production_read_only_observation_operator.py"
    sealed_release_operator = artifacts / "immutable_release_operator.py"
    sealed_observer.write_bytes(Path(operator.__file__).read_bytes())
    sealed_release_operator.write_bytes(Path(operator.release_operator.__file__).read_bytes())
    sealed_observer.chmod(0o400)
    sealed_release_operator.chmod(0o400)
    artifacts.chmod(0o500)
    interpreter.parent.chmod(0o500)
    interpreter.parent.parent.chmod(0o500)
    release_root.chmod(0o500)
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    harness = textwrap.dedent(
        """
        import hashlib
        import importlib.util
        import json
        import pathlib
        import sys
        import types

        script = pathlib.Path(sys.argv[1])
        output = pathlib.Path(sys.argv[2])
        spec = importlib.util.spec_from_file_location("sealed_observer", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        release_root = script.parent.parent
        events = []
        release = module.release_operator.ReleaseIdentity(
            root=release_root,
            commit="a" * 40,
            version="0.207.97",
            tree_manifest_sha256="b" * 64,
            max_schema=50,
            production_observation_operator_sha256=(
                hashlib.sha256(script.read_bytes()).hexdigest()
            ),
        )

        def load_release(root, *, expected_tree_sha256):
            assert root == release_root
            assert expected_tree_sha256 == "b" * 64
            events.append("load")
            return release

        def systemd_config(args):
            assert args.output == output
            events.append("config")
            return object()

        def publish(actual_release, config, actual_output):
            assert actual_release is release
            assert config is not None
            assert actual_output == output
            events.append("publish")
            return {
                "artifact_sha256": "c" * 64,
                "endpoint_response_sha256": "d" * 64,
                "release_binding_sha256": "e" * 64,
            }

        module.release_operator.load_release_identity = load_release
        module.release_operator._systemd_config = systemd_config
        module.publish_authenticated_release_captain_artifact = publish
        receipt = module.execute(
            types.SimpleNamespace(release_tree_sha256="b" * 64, output=output)
        )
        print(json.dumps({"events": events, "receipt": receipt}, sort_keys=True))
        """
    )
    completed = subprocess.run(  # noqa: S603
        [
            str(interpreter),
            "-I",
            "-B",
            "-c",
            harness,
            str(sealed_observer),
            str(target),
        ],
        cwd=foreign_cwd,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stderr == b""
    positive = json.loads(completed.stdout)
    assert positive["events"] == ["load", "config", "publish"]
    assert positive["receipt"] == {
        "artifact_sha256": "c" * 64,
        "endpoint_response_sha256": "d" * 64,
        "release_binding_sha256": "e" * 64,
        "schema": operator.ARTIFACT_SCHEMA,
        "status": "clear",
    }
    assert not target.exists()
    assert not tuple(release_root.rglob("__pycache__"))

    forged = object.__new__(operator.SystemdReleaseCaptainRuntime)
    forged.authenticate_release = lambda: dict(_RELEASE)
    forged.process_epoch_sha256 = lambda: _EPOCH
    forged.accepted_health_bytes = lambda: b'{"status":"ok"}'
    forged.observation_bytes = lambda challenge: operator.canonical_json_bytes(_observation(challenge))

    with pytest.raises(
        operator.ProductionObservationOperatorError,
        match="runtime_boundary_invalid",
    ):
        operator.publish_authenticated_release_captain_artifact(  # type: ignore[arg-type]
            forged,
            SimpleNamespace(),
            target,
        )

    assert not target.exists()
    assert "SystemdReleaseCaptainRuntime" not in operator.__all__
    assert "_ReleaseCaptainRuntime" not in operator.__all__
    assert "_build_release_captain_artifact" not in operator.__all__
    assert "_write_artifact_create_only" not in operator.__all__


def test_concrete_health_digest_bytes_receive_the_full_immutable_health_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_operator = operator.release_operator

    raw = b'{"private":"PRIVATE-HEALTH-BODY","status":"ok","version":"0.207.95"}'
    release = release_operator.ReleaseIdentity(
        root=Path("/release"),
        commit="a" * 40,
        version="0.207.95",
        tree_manifest_sha256="b" * 64,
        max_schema=50,
    )

    class Port:
        def __init__(self) -> None:
            self.waited = False

        def accept_backend(self, actual_release) -> None:
            assert actual_release is release

        def _expected_semantic_health_mode(self) -> str:
            return ""

        def _expected_semantic_effect_health(self):
            return "", None

        def _wait_process(self, unit, actual_release, role) -> None:
            assert (unit, actual_release, role) == ("friday-backend.service", release, "backend")
            self.waited = True

    port = Port()
    runtime = object.__new__(operator.SystemdReleaseCaptainRuntime)
    runtime.release = release
    runtime.config = SimpleNamespace(
        health_url="https://127.0.0.1:8000/api/health",
        memory_vault_mode="full_owner",
        obsidian_mode="enabled",
        backend_unit="friday-backend.service",
    )
    runtime._port = port  # noqa: SLF001
    monkeypatch.setattr(runtime, "_get", lambda *_args, **_kwargs: raw)
    observed: list[dict[str, object]] = []

    def memory_match(value, *_args):
        observed.append(value)
        return False

    monkeypatch.setattr(release_operator, "_memory_vault_health_identity_matches", memory_match)
    monkeypatch.setattr(release_operator, "_obsidian_health_identity_matches", lambda *_args: True)
    monkeypatch.setattr(release_operator, "_obsidian_root_sha256", lambda *_args: "c" * 64)

    with pytest.raises(operator.ProductionObservationOperatorError, match="health_response_invalid"):
        runtime.accepted_health_bytes()

    assert observed == [json.loads(raw)]
    assert port.waited is False

    monkeypatch.setattr(
        release_operator,
        "_memory_vault_health_identity_matches",
        lambda *_args: True,
    )
    assert runtime.accepted_health_bytes() == raw
    assert port.waited is True
