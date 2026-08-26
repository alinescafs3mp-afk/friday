"""Static release contracts for the optional Ubuntu Host Control deployment."""

from __future__ import annotations

import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tomllib

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "host-control"


def _text(relative: str) -> str:
    return (DEPLOY / relative).read_text(encoding="utf-8")


def test_compose_override_is_narrow_and_every_feature_defaults_off() -> None:
    compose = yaml.safe_load(_text("compose.override.yml"))
    backend = compose["services"]["backend"]
    environment = backend["environment"]
    disabled = {
        "FRIDAY_HOST_CONTROL_ENABLED",
        "FRIDAY_HOST_PACKAGE_INSTALL_ENABLED",
        "FRIDAY_HOST_DESKTOP_CONTROL_ENABLED",
        "FRIDAY_HOST_ONE_SHOT_EXEC_ENABLED",
        "FRIDAY_HOST_PUBLIC_NETWORK_ENABLED",
    }
    assert all(str(environment[name]).endswith(":-0}") for name in disabled)
    assert backend["read_only"] is True
    assert backend["cap_drop"] == ["ALL"]
    assert backend["security_opt"] == ["no-new-privileges:true"]
    assert backend["userns_mode"] == "host"
    assert backend["user"] == (
        "${FRIDAY_HOST_RUNTIME_UID:?use the UID printed by the host-control installer}:"
        "${FRIDAY_HOST_RUNTIME_GID:?use the GID printed by the host-control installer}"
    )
    assert backend["build"] == {
        "context": ".",
        "dockerfile": "docker/Dockerfile.backend",
        "args": {
            "FRIDAY_RUNTIME_UID": "${FRIDAY_HOST_RUNTIME_UID:?use the UID printed by the host-control installer}",
            "FRIDAY_RUNTIME_GID": "${FRIDAY_HOST_RUNTIME_GID:?use the GID printed by the host-control installer}",
        },
    }
    assert backend["group_add"] == [
        "${FRIDAY_HOST_APPROVAL_SIGNER_GID:?use the signer GID printed by the host-control installer}"
    ]
    assert backend["ulimits"]["core"] == {"soft": 0, "hard": 0}
    assert backend.get("privileged") is not True
    assert environment["FRIDAY_HOST_AGENT_ID"] == "local-user-agent"
    assert environment["FRIDAY_HOST_APPROVAL_SIGNING_KEY_FILE"] == ("/run/friday-backend-approval.key")

    mounts = {item["target"]: item for item in backend["volumes"]}
    assert set(mounts) == {
        "/run/friday-host-agent",
        "/run/friday-backend-approval.key",
        "/runtime/data/host-control/jobs",
    }
    assert mounts["/run/friday-host-agent"]["read_only"] is True
    assert all(item["bind"]["create_host_path"] is False for item in mounts.values())

    secret = backend["secrets"]
    assert secret == [
        {
            "source": "friday_host_agent_hmac",
            "target": "friday_host_agent_hmac",
            "uid": "${FRIDAY_HOST_RUNTIME_UID:?use the UID printed by the host-control installer}",
            "gid": "${FRIDAY_HOST_RUNTIME_GID:?use the GID printed by the host-control installer}",
            "mode": 0o400,
        }
    ]
    rendered = repr(compose).casefold()
    for forbidden in (
        "/var/run/docker.sock",
        "/run/user:/run/user",
        "/etc:/etc",
        "/usr:/usr",
        "/home:/home",
        "/run/dbus/system_bus_socket",
        "broker.key:/",
    ):
        assert forbidden not in rendered


def test_backend_image_contains_the_engineer_sandbox_runtime() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends bubblewrap python3-minimal" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile


def test_host_agent_user_unit_is_unprivileged_and_secret_minimal() -> None:
    unit = _text("systemd/user/friday-host-agent.service")
    assert "ExecStart=/usr/bin/env --ignore-environment " in unit
    assert " /opt/friday-host-control/current/bin/friday-host-agent " in unit
    assert "--allowed-peer-uid ${FRIDAY_HOST_AGENT_ALLOWED_PEER_UID}" in unit
    assert "--socket ${FRIDAY_HOST_AGENT_SOCKET}" in unit
    assert "--agent-id ${FRIDAY_HOST_AGENT_ID}" in unit
    assert "--build-id ${FRIDAY_HOST_CONTROL_BUILD_ID}" in unit
    assert "--max-concurrency ${FRIDAY_HOST_AGENT_MAX_CONCURRENCY}" in unit
    assert "--network-policy /etc/friday-host-control/host-agent-policy.toml" in unit
    assert (
        "--network-approval-public-key-file /etc/friday-host-control/backend-approval-signing.pub"
    ) in unit
    assert "--broker-signing-public-key-file /etc/friday-host-control/broker-signing.pub" in unit
    assert "broker-signing.key" not in unit
    assert "EnvironmentFile=/etc/friday-host-control/host-agent.env" in unit
    assert "EnvironmentFile=/etc/friday-host-control/release.env" in unit
    assert "ConditionPathExists=%h/.config/friday-host-agent/agent.key" in unit
    assert "ConditionPathExists=/etc/friday-host-control/host-agent-policy.toml" in unit
    assert "ConditionPathExists=/etc/friday-host-control/backend-approval-signing.pub" in unit
    assert "User=root" not in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "InaccessiblePaths=/etc/friday-host-control/backend-approval-signing.key" in unit
    assert "PrivateNetwork=yes" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "RuntimeDirectory=" not in unit
    assert "StateDirectoryMode=0700" in unit
    assert "TasksMax=64" in unit
    assert "MemoryMax=512M" in unit
    assert "KillSignal=SIGINT" in unit
    for secret in (
        "FRIDAY_API_TOKEN",
        "FRIDAY_TELEGRAM_BOT_TOKEN",
        "FRIDAY_TELEGRAM_BRIDGE_SECRET",
        "FRIDAY_LLM_API_KEY",
    ):
        assert secret in unit.split("UnsetEnvironment=", 1)[1]
    assert "/bin/sh" not in unit
    assert "/bin/bash" not in unit


def test_package_broker_units_have_one_fixed_root_surface() -> None:
    service = _text("systemd/system/friday-package-broker.service")
    socket = _text("systemd/system/friday-package-broker.socket")
    assert "User=root" in service
    assert "ExecStart=/opt/friday-host-control/current/bin/friday-package-broker " in service
    assert "UnsetEnvironment=FRIDAY_API_TOKEN " in service
    assert "--systemd-socket" in service
    assert "--policy /etc/friday-host-control/broker-policy.toml" in service
    assert "--key-file /etc/friday-host-control/broker.key" in service
    assert "--signing-key-file /etc/friday-host-control/broker-signing.key" in service
    assert (
        "--approval-verification-public-key-file /etc/friday-host-control/backend-approval-signing.pub"
    ) in service
    assert "--build-id ${FRIDAY_HOST_CONTROL_BUILD_ID}" in service
    assert "EnvironmentFile=/etc/friday-host-control/release.env" in service
    assert "host-agent.env" not in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectHome=yes" in service
    assert "PrivateDevices=yes" in service
    assert "KillSignal=SIGINT" in service
    assert "TimeoutStopSec=900s" in service
    assert "CAP_SYS_ADMIN" not in service
    assert "CAP_NET_ADMIN" not in service
    assert "CAP_SYS_PTRACE" not in service
    assert "\n[Install]\n" not in service
    assert "/bin/sh" not in service
    assert "/bin/bash" not in service
    assert "\nEnvironment=FRIDAY_API_TOKEN" not in service
    assert "FRIDAY_DATABASE" not in service

    assert "ListenStream=/run/friday-package-broker/broker.sock" in socket
    assert "SocketMode=0660" in socket
    assert "DirectoryMode=0711" in socket
    assert "SocketGroup=root" in socket  # fail closed until installer's exact drop-in
    assert "Accept=no" in socket
    assert "Service=friday-package-broker.service" in socket


def test_broker_example_is_a_closed_nmap_only_policy() -> None:
    policy = tomllib.loads(_text("examples/policy.toml"))["broker"]
    assert set(policy) == {
        "broker_id",
        "allowed_peer_uids",
        "allowed_packages",
        "max_package_changes",
        "max_download_bytes",
        "max_installed_delta_bytes",
        "plan_ttl_sec",
    }
    assert policy["allowed_peer_uids"] == [1000]
    assert policy["allowed_packages"] == ["nmap"]
    assert policy["max_package_changes"] <= 32
    assert policy["plan_ttl_sec"] <= 900


def test_host_agent_example_policy_starts_with_no_network_authority() -> None:
    policy = tomllib.loads(_text("examples/host-agent-policy.toml"))["network"]
    assert policy == {
        "schema_version": 1,
        "allowed_cidrs": [],
        "allow_public": False,
    }


def test_operator_scripts_are_effect_explicit_and_fixed_path() -> None:
    install = DEPLOY / "install.sh"
    uninstall = DEPLOY / "uninstall.sh"
    verifier = DEPLOY / "verify_wheel.py"
    user_assets = DEPLOY / "prepare_user_assets.py"
    assert stat.S_IMODE(install.stat().st_mode) == 0o755
    assert stat.S_IMODE(uninstall.stat().st_mode) == 0o755
    assert stat.S_IMODE(verifier.stat().st_mode) == 0o755
    assert stat.S_IMODE(user_assets.stat().st_mode) == 0o755
    install_text = install.read_text(encoding="utf-8")
    uninstall_text = uninstall.read_text(encoding="utf-8")
    for source in (install_text, uninstall_text):
        assert source.startswith("#!/bin/sh\nset -eu\n")
        lowered = source.casefold()
        for forbidden in (
            "sudo ",
            "eval ",
            "bash -c",
            "sh -c",
            "docker.sock",
            "nopasswd",
            "apt-get install",
        ):
            assert forbidden not in lowered

    assert "--backend-uid" not in install_text
    assert '"FRIDAY_HOST_AGENT_ALLOWED_PEER_UID=$USER_UID"' in install_text
    assert '"FRIDAY_HOST_AGENT_ID=local-user-agent"' in install_text
    assert '"FRIDAY_HOST_AGENT_SOCKET=$SOCKET_DIR/agent.sock"' in install_text
    assert "selected user UID/GID must be at least 1000 for the container mapping" in install_text
    assert '"  FRIDAY_HOST_RUNTIME_UID=$USER_UID"' in install_text
    assert '"  FRIDAY_HOST_RUNTIME_GID=$USER_GID"' in install_text
    assert "--friday-data-dir is required" in install_text
    assert "--system-site-packages" in install_text
    assert "import apt, apt_pkg" in install_text
    assert 'version("cryptography")' in install_text
    assert "sys.version_info < (3, 11)" in install_text
    assert "release >= (41, 0, 7) and release < (51, 0, 0)" in install_text
    assert "Ed25519PrivateKey.generate()" in install_text
    assert "Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, payload)" in install_text
    assert "broker-signing.key" in install_text
    assert "broker-signing.pub" in install_text
    assert "backend-approval-signing.key" in install_text
    assert "backend-approval-signing.pub" in install_text
    assert "APPROVAL_SIGNER_GROUP=friday-host-approval" in install_text
    assert '"  FRIDAY_HOST_APPROVAL_SIGNER_GID=$APPROVAL_SIGNER_GID"' in install_text
    assert "load_backend_approval_signing_key" in install_text
    assert "FRIDAY_HOST_CONTROL_BUILD_ID=%s" in install_text
    assert '"$CANDIDATE_VENV/bin/friday-package-broker" \\' in install_text
    assert "    --check-config \\" in install_text
    assert "stop the package broker socket/service before install or upgrade" in install_text
    assert "stop the host-agent user service before install or upgrade" in install_text
    assert "--artifact-wheel is required" in install_text
    assert "--artifact-sha256 is required" in install_text
    assert '"$SCRIPT_DIR/verify_wheel.py" "$wheel_temp" "$ARTIFACT_SHA256"' in install_text
    assert "--no-index --no-deps --force-reinstall" in install_text
    assert "--no-build-isolation" not in install_text
    assert "SOURCE_ROOT" not in install_text
    assert "allowed_peer_uids = [$USER_UID]" in install_text
    assert 'HOST_AGENT_POLICY="$CONTROL_DIR/host-agent-policy.toml"' in install_text
    assert '"$SCRIPT_DIR/examples/host-agent-policy.toml" "$HOST_AGENT_POLICY"' in install_text
    assert "ReadWritePaths=%s" in install_text
    assert '"$SOCKET_DIR" "$FRIDAY_DATA_DIR/host-control/jobs"' in install_text
    assert "SocketGroup=%s" in install_text
    assert 'prepare_directory "$SOCKET_BASE_DIR" 0 root root 711' in install_text
    assert 'prepare_directory "$SOCKET_DIR" "$USER_UID" "$TARGET_USER" "$USER_GROUP" 700' in install_text
    assert 'prepare_directory "$USER_HOME/' not in install_text
    assert 'prepare_directory "$FRIDAY_DATA_DIR/' not in install_text
    assert 'as_target_user /usr/bin/python3 -I "$SCRIPT_DIR/prepare_user_assets.py"' in install_text
    assert "attest_root_venv_tree" in install_text
    assert 'NEW_RELEASE_DIR=$(/usr/bin/mktemp -d "$RELEASES_DIR/$ARTIFACT_SHA256.XXXXXX")' in install_text
    pip_install = install_text.index('"$CANDIDATE_VENV/bin/python" -m pip --isolated install')
    entrypoint_check = install_text.index('[ -x "$CANDIDATE_VENV/bin/friday-host-agent" ]')
    assert "attest_root_venv_tree" in install_text[pip_install:entrypoint_check]
    assert install_text.count('--policy "$CONTROL_DIR/broker-policy.toml"') == 1
    assert 'as_target_user "$CANDIDATE_VENV/bin/friday-host-agent" \\' in install_text
    assert '    --network-policy "$HOST_AGENT_POLICY" \\' in install_text
    assert '    --network-approval-public-key-file "$APPROVAL_SIGNING_PUBLIC_KEY" \\' in install_text
    assert '"0:$USER_GID:640:32"' in install_text
    assert '/usr/bin/install -o root -g "$USER_GROUP" -m 0640 \\' in install_text
    assert "installed host-agent configuration failed its unprivileged preflight" in install_text
    assert '/usr/bin/systemd-tmpfiles --create "$TMPFILES_CONFIG"' in install_text
    assert '"  FRIDAY_HOST_AGENT_SOCKET_DIR_HOST=$SOCKET_DIR"' in install_text
    assert 'if [ "$ENABLE_SERVICES" -eq 1 ]' in install_text

    user_asset_text = user_assets.read_text(encoding="utf-8")
    assert "must run as the selected non-root user" in user_asset_text
    assert "dir_fd=" in user_asset_text
    assert "os.O_NOFOLLOW" in user_asset_text
    assert "os.O_EXCL" in user_asset_text
    assert "os.fchmod" in user_asset_text

    assert "--purge-secrets" in uninstall_text
    assert "/opt/friday-host-control -xdev -depth -delete" in uninstall_text
    assert "host-control/jobs" not in uninstall_text
    assert "loginctl disable-linger" not in uninstall_text
    assert "package broker did not drain and stop; no files were removed" in uninstall_text
    assert "/etc/tmpfiles.d/friday-host-agent.conf" in uninstall_text
    assert "/etc/friday-host-control/host-agent-policy.toml" in uninstall_text


def test_installer_stages_then_atomically_activates_and_has_exact_rollback_inventory() -> None:
    source = _text("install.sh")
    activation = source.index('/usr/bin/mv -fT -- "$CURRENT_STAGE" "$CURRENT_LINK"')
    commit = source.rindex("COMMITTED=1")

    for staged_before_activation in (
        '"$CANDIDATE_VENV/bin/python" -m pip --isolated install',
        '"$CANDIDATE_VENV/bin/friday-package-broker" \\',
        'as_target_user "$CANDIDATE_VENV/bin/friday-host-agent" \\',
        "HOST_AGENT_UNIT_STAGE=$(/usr/bin/mktemp",
        'snapshot_file "$CONTROL_DIR/host-agent.env" host_agent_env',
    ):
        assert source.index(staged_before_activation) < activation
    publication = source[source.index("CURRENT_STAGE=") : source.index('activate_file "$ENV_STAGE"')]
    assert publication.index("journal_write phase publication_armed") < publication.index("ACTIVATED=1")
    assert publication.index("ACTIVATED=1") < publication.index(
        '/usr/bin/mv -fT -- "$CURRENT_STAGE" "$CURRENT_LINK"'
    )
    assert publication.index("journal_write phase published") > publication.index(
        '/usr/bin/mv -fT -- "$CURRENT_STAGE" "$CURRENT_LINK"'
    )
    assert source.index("rollback_transaction()") < source.index("on_exit()")
    assert "rollback_transaction; then" in source
    assert source.index("package broker socket did not become active") < commit
    assert source.index("host-agent user service did not become active") < commit
    assert source.index("journal_write phase committed") < commit

    rollback = source[source.index("rollback_transaction()") : source.index("on_exit()")]
    assert rollback.index("restore_enablement") < rollback.index('restore_file "$CONTROL_DIR/host-agent.env"')
    enablement = source[source.index("restore_enablement()") : source.index("rollback_transaction()")]
    assert "enablement_failed=0" in enablement
    assert "return 1" not in enablement
    assert enablement.count("|| enablement_failed=1") == 6
    restored = {
        "host_agent_env",
        "release_env",
        "tmpfiles",
        "host_agent_unit",
        "broker_service",
        "broker_socket",
        "host_agent_dropin",
        "broker_socket_dropin",
        "readme",
    }
    assert all(f" {tag}" in rollback for tag in restored)
    for preserved in (
        "broker.key",
        "broker-signing.key",
        "backend-approval-signing.key",
        "broker-policy.toml",
        "host-agent-policy.toml",
        "host-control/jobs",
    ):
        assert preserved not in rollback


def test_installer_recovery_journal_and_signal_cleanup_are_fail_closed() -> None:
    source = _text("install.sh")
    detection = source.index('if [ -e "$TRANSACTION_DIR" ] || [ -L "$TRANSACTION_DIR" ]')
    assert detection < source.index("groupadd --system")
    assert detection < source.index('prepare_directory "$CONTROL_DIR"')
    assert "TRANSACTION_DIR=$INSTALL_DIR/.install-transaction" in source
    assert "--recover accepts only --user" in source
    assert "recover_stale_transaction\n    exit 0" in source
    recovery = source[source.index("recover_stale_transaction()") : detection]
    assert "eval " not in recovery
    assert "journal_read target_user" in recovery
    assert "journal_read candidate_venv" in recovery
    assert 'if [ "$TRANSACTION_PHASE" = committed ]' in recovery
    assert "journal_read broker_enable_attempted" not in recovery  # closed loop, no free-form parser
    assert "for journal_boolean in broker_was_enabled" in recovery

    journal = source[source.index("journal_write()") : source.index("CURRENT_STAGE=")]
    for required in (
        "journal_write phase prepared",
        'journal_write candidate_venv "$CANDIDATE_VENV"',
        'journal_write current_was_present "$CURRENT_WAS_PRESENT"',
        "journal_write broker_enable_attempted 0",
        "journal_write user_enable_attempted 0",
        "journal_write linger_attempted 0",
    ):
        assert required in journal
    enable = source[source.rindex('if [ "$ENABLE_SERVICES" -eq 1 ]') : source.rindex("COMMITTED=1")]
    assert enable.index("journal_write linger_attempted 1") < enable.index(
        '/usr/bin/loginctl enable-linger "$TARGET_USER"'
    )
    assert enable.index("journal_write broker_enable_attempted 1") < enable.index(
        "/usr/bin/systemctl enable --now friday-package-broker.socket"
    )
    assert enable.index("journal_write user_enable_attempted 1") < enable.index(
        "as_user_systemctl enable --now friday-host-agent.service"
    )

    on_exit = source[source.index("on_exit()") : source.index("trap on_exit EXIT")]
    assert "trap '' HUP INT TERM\n    trap - EXIT" in on_exit
    assert "trap - EXIT HUP INT TERM" not in on_exit
    assert "NEW_RELEASE_DIR=" in on_exit
    rollback_failure = on_exit[
        on_exit.index("if ! rollback_transaction") : on_exit.index('if [ "$COMMITTED" -eq 0 ]')
    ]
    assert "status=125" in rollback_failure
    assert "explicit recovery proves that removal is safe" in rollback_failure
    current_validation = source[
        source.index('if [ -e "$CURRENT_LINK"') : source.index('if [ -e "$LEGACY_VENV_DIR"')
    ]
    assert '/usr/bin/find -P "$CURRENT_LINK"' in current_validation
    assert '/usr/bin/stat -c %u -- "$CURRENT_LINK"' not in current_validation
    assert '/usr/bin/sync -f "$ROLLBACK_DIR/meta.$journal_name"' in source
    assert '/usr/bin/sync -f "$INSTALL_DIR"' in source


def test_installer_restore_file_shell_function_restores_bytes_mode_and_absence(
    tmp_path: pathlib.Path,
) -> None:
    source = _text("install.sh")
    start = source.index("restore_file() {")
    end = source.index("\n}\n\nrestore_enablement()", start) + 2
    function = source[start:end]
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    target = tmp_path / "unit.conf"
    target.write_text("new\n", encoding="utf-8")
    target.chmod(0o600)
    (rollback / "unit.file").write_text("old\n", encoding="utf-8")
    (rollback / "unit.file").chmod(0o640)
    (rollback / "unit.state").write_text("present\n", encoding="ascii")

    command = "\n".join(
        (
            "set -eu",
            function,
            f"ROLLBACK_DIR={shlex.quote(str(rollback))}",
            f"restore_file {shlex.quote(str(target))} unit",
        )
    )
    completed = subprocess.run(["/bin/sh", "-c", command], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert target.read_text(encoding="utf-8") == "old\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    (rollback / "unit.state").write_text("absent\n", encoding="ascii")
    target.write_text("new-again\n", encoding="utf-8")
    completed = subprocess.run(["/bin/sh", "-c", command], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert not target.exists()

    (rollback / "unit.state").write_text("present\n", encoding="ascii")
    (rollback / "unit.file").unlink()
    target.write_text("still-live\n", encoding="utf-8")
    completed = subprocess.run(["/bin/sh", "-c", command], check=False, capture_output=True, text=True)
    assert completed.returncode != 0
    assert not tuple(tmp_path.glob(".friday-rollback.*"))


@pytest.mark.skipif(os.geteuid() == 0, reason="cleanup failure injection requires non-root")
def test_installer_cleanup_shell_function_is_best_effort(tmp_path: pathlib.Path) -> None:
    source = _text("install.sh")
    start = source.index("cleanup_paths() {")
    end = source.index("\n}\n\nrestore_file()", start) + 2
    function = source[start:end]
    blocked_parent = tmp_path / "blocked"
    blocked_parent.mkdir()
    blocked = blocked_parent / "one"
    blocked.write_text("blocked", encoding="utf-8")
    removable = tmp_path / "two"
    removable.write_text("remove", encoding="utf-8")
    blocked_parent.chmod(0o500)
    try:
        command = "\n".join(
            (
                "set -eu",
                function,
                f"TEMP_PATHS='{shlex.quote(str(blocked))} {shlex.quote(str(removable))}'",
                "TEMP_DIRS=",
                "cleanup_paths",
            )
        )
        completed = subprocess.run(["/bin/sh", "-c", command], check=False, capture_output=True, text=True)
    finally:
        blocked_parent.chmod(0o700)
    assert completed.returncode != 0
    assert blocked.exists()
    assert not removable.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="helper intentionally rejects root")
def test_user_asset_helper_creates_only_sealed_owner_assets(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir(mode=0o700)
    data.mkdir(mode=0o700)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(DEPLOY / "prepare_user_assets.py"),
            "--home",
            str(home),
            "--data-dir",
            str(data),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    expected_directories = (
        home / ".config" / "friday-host-agent",
        home / ".local" / "state" / "friday-host-agent",
        data / "host-control",
        data / "host-control" / "jobs",
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in expected_directories)
    key = home / ".config" / "friday-host-agent" / "agent.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.stat().st_nlink == 1
    assert len(key.read_bytes()) == 48


@pytest.mark.skipif(os.geteuid() == 0, reason="helper intentionally rejects root")
def test_user_asset_helper_refuses_symlinked_chain(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    home.mkdir(mode=0o700)
    data.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (home / ".config").symlink_to(outside, target_is_directory=True)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(DEPLOY / "prepare_user_assets.py"),
            "--home",
            str(home),
            "--data-dir",
            str(data),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "user asset preparation rejected" in completed.stderr
    assert not (outside / "friday-host-agent").exists()


def test_deployment_documentation_keeps_the_activation_honest() -> None:
    readme = _text("README.md")
    assert "remain independently disabled" in readme
    assert "startup validation fail closed" in readme
    assert "There is no host shell" in readme
    assert "create_host_path: false" in readme
    assert "FRIDAY_HOST_CONTROL_ENABLED=0" in readme
    assert "FRIDAY_HOST_PACKAGE_INSTALL_ENABLED=0" in readme
    assert "Keep desktop and one-shot flags at `0`" in readme
    assert "Never use the\nuninstall script to erase those acceptance receipts" in readme
    assert "rootful Docker Compose" in readme
    assert "not by this optional\nUnix-socket override" in readme
    assert "it never admits host root" in readme
    assert "same directory mounted at `/runtime/data`" in readme
    assert "Stopping the agent removes only `agent.sock`, not the bind-source" in readme
    assert "up -d --force-recreate backend" in readme
    assert "must report Host Control unavailable" in readme
    assert "Do not proceed if either value differs" in readme
    assert "otherwise memberless supplemental signer GID" in readme
    enable_later = readme[readme.index("To enable later:") : readme.index("Linger keeps")]
    assert 'sudo systemctl start "user@$FRIDAY_UID.service"' in enable_later
    assert enable_later.count('sudo /usr/sbin/runuser -u "$FRIDAY_USER" -- /usr/bin/env -i') == 2
    for exact_environment in (
        'HOME="$FRIDAY_HOME" USER="$FRIDAY_USER" LOGNAME="$FRIDAY_USER"',
        'PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$FRIDAY_UID"',
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$FRIDAY_UID/bus"',
    ):
        assert enable_later.count(exact_environment) == 2
    assert "\nsystemctl --user " not in enable_later
    assert "/opt/friday-host-control/current/bin/friday-package-broker" in readme
    assert "any installer failure or handled signal\nrestores the previous activation" in readme
    assert "/opt/friday-host-control/.install-transaction" in readme
    assert "--recover --user friday" in readme
    assert "do not delete or edit the journal" in readme
    assert "without evaluating journal text" in readme


def test_offline_ubuntu_crypto_floor_matches_project_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    crypto = [item for item in project["dependencies"] if item.startswith("cryptography")]
    assert crypto == ["cryptography>=41.0.7,<51"]

    readme = _text("README.md")
    assert "Ubuntu 24.04 LTS or newer" in readme
    assert "Python >=3.11" in readme
    assert "`cryptography>=41.0.7,<51`" in readme


def test_backend_image_build_contains_host_control_and_supports_exact_non_root_ids() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "ARG FRIDAY_RUNTIME_UID" in dockerfile
    assert "ARG FRIDAY_RUNTIME_GID" in dockerfile
    assert '[ "$FRIDAY_RUNTIME_UID" -ge 1000 ]' in dockerfile
    assert '[ "$FRIDAY_RUNTIME_GID" -ge 1000 ]' in dockerfile
    assert '--uid "$FRIDAY_RUNTIME_UID" --gid jericho' in dockerfile

    install_at = dockerfile.index("python -m pip install .")
    assert "COPY pyproject.toml README.md LICENSE requirements.lock ./" in dockerfile
    assert dockerfile.index("COPY pyproject.toml README.md LICENSE requirements.lock ./") < install_at
    for package in ("friday_host_agent", "friday_package_broker"):
        copy_line = f"COPY {package}/ ./{package}/"
        assert copy_line in dockerfile
        assert dockerfile.index(copy_line) < install_at


def test_tmpfiles_contract_keeps_socket_parent_private_and_independent_of_agent() -> None:
    template = _text("systemd/tmpfiles/friday-host-agent.conf.in").splitlines()
    assert template[-2:] == [
        "d /run/friday-host-agent 0711 root root -",
        "d /run/friday-host-agent/@USER_UID@ 0700 @USER_UID@ @USER_GID@ -",
    ]
    unit = _text("systemd/user/friday-host-agent.service")
    assert "RuntimeDirectory=" not in unit
