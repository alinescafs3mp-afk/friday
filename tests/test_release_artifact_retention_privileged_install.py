from __future__ import annotations

import hashlib
import os
import pwd
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/release-retention/install-privileged-proc-probe.sh"
UNINSTALLER = ROOT / "deploy/release-retention/uninstall-privileged-proc-probe.sh"
HELPER_SOURCE = ROOT / "tools/release_artifact_proc_probe.py"


def _run(
    script: Path, *arguments: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = {
        "FRIDAY_RETENTION_INSTALL_TEST_MODE": "1",
        "HOME": str(ROOT),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603
        [str(script), *arguments],
        capture_output=True,
        check=False,
        env=env,
        timeout=15,
    )


def test_privileged_probe_installer_is_atomic_exact_and_rollback_safe(tmp_path: Path) -> None:
    for script in (INSTALLER, UNINSTALLER):
        syntax = subprocess.run(  # noqa: S603
            ["/bin/sh", "-n", str(script)],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert syntax.returncode == 0, syntax.stderr

    fake_root = tmp_path / "root"
    fake_root.mkdir()
    fake_root.chmod(0o700)
    owner = pwd.getpwuid(os.geteuid()).pw_name
    installed = _run(
        INSTALLER,
        "--owner-user",
        owner,
        "--source",
        str(HELPER_SOURCE),
        "--expected-sha256",
        hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest(),
        "--root",
        str(fake_root),
    )
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout == b"friday_retention_probe_installed\n"

    helper = fake_root / "usr/libexec/friday/release_artifact_proc_probe.py"
    sudoers = fake_root / "etc/sudoers.d/friday-retention-probe"
    assert helper.read_bytes() == HELPER_SOURCE.read_bytes()
    assert helper.stat().st_uid == os.geteuid()
    assert helper.stat().st_mode & 0o7777 == 0o755
    assert sudoers.stat().st_uid == os.geteuid()
    assert sudoers.stat().st_mode & 0o7777 == 0o440
    python = Path("/usr/bin/python3").resolve(strict=True)
    assert sudoers.read_text(encoding="ascii") == (
        f"#{os.geteuid()} ALL=(root) NOPASSWD: {python} -I -B "
        "-S "
        "/usr/libexec/friday/release_artifact_proc_probe.py privileged-target-probe\n"
    )

    changed_source = tmp_path / "changed-probe.py"
    changed_source.write_bytes(HELPER_SOURCE.read_bytes() + b"\n")
    changed_sha256 = hashlib.sha256(changed_source.read_bytes()).hexdigest()
    failed = _run(
        INSTALLER,
        "--owner-user",
        owner,
        "--source",
        str(changed_source),
        "--expected-sha256",
        changed_sha256,
        "--root",
        str(fake_root),
        extra_env={"FRIDAY_RETENTION_INSTALL_FAIL_AFTER_HELPER": "1"},
    )
    assert failed.returncode == 2
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == changed_sha256
    assert not sudoers.exists()  # grant stays revoked throughout crash recovery

    recovered = _run(
        INSTALLER,
        "--owner-user",
        owner,
        "--source",
        str(changed_source),
        "--expected-sha256",
        changed_sha256,
        "--root",
        str(fake_root),
    )
    assert recovered.returncode == 0, recovered.stderr
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == changed_sha256
    assert sudoers.exists()
    assert not list((fake_root / "usr/libexec/friday").glob("*.new"))
    assert not list((fake_root / "usr/libexec/friday").glob("*.revoked"))
    assert not list((fake_root / "etc/sudoers.d").glob("*.new"))
    assert not list((fake_root / "etc/sudoers.d").glob("*.revoked"))

    uninstalling = fake_root / "etc/sudoers.d/.friday-retention-probe.uninstalling"
    uninstalling.write_bytes(sudoers.read_bytes())
    uninstalling.chmod(0o440)
    blocked_cross_transaction = _run(
        INSTALLER,
        "--owner-user",
        owner,
        "--source",
        str(changed_source),
        "--expected-sha256",
        changed_sha256,
        "--root",
        str(fake_root),
    )
    assert blocked_cross_transaction.returncode == 2

    removed = _run(UNINSTALLER, "--root", str(fake_root))
    assert removed.returncode == 0, removed.stderr
    assert removed.stdout == b"friday_retention_probe_uninstalled\n"
    assert not helper.exists()
    assert not sudoers.exists()
    assert not uninstalling.exists()

    fake_root.chmod(0o770)
    unsafe_root = _run(
        INSTALLER,
        "--owner-user",
        owner,
        "--source",
        str(HELPER_SOURCE),
        "--expected-sha256",
        hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest(),
        "--root",
        str(fake_root),
    )
    assert unsafe_root.returncode == 2

    fake_root.chmod(0o700)
    rejected_wildcard = _run(
        INSTALLER,
        "--owner-user",
        "ALL",
        "--source",
        str(HELPER_SOURCE),
        "--expected-sha256",
        hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest(),
        "--root",
        str(fake_root),
    )
    assert rejected_wildcard.returncode == 2
