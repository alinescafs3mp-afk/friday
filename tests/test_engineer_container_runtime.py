"""Release contracts for the opt-in Ubuntu Engineer container boundary."""

from __future__ import annotations

import hashlib
import json
import pathlib
import stat
import subprocess

import pytest

from friday.organs.engineer import sandbox
from friday_host_agent.inventory import DpkgPackageResolver

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "engineer-mode"


def _text(relative: str) -> str:
    return (DEPLOY / relative).read_text(encoding="utf-8")


def _seccomp_rules(name: str, *, action: str | None = None) -> list[dict[str, object]]:
    profile = json.loads(_text("seccomp.json"))
    return [
        rule
        for rule in profile["syscalls"]
        if name in rule["names"] and (action is None or rule["action"] == action)
    ]


def _require_package_file(path: pathlib.Path, package: str, *, executable: bool = False) -> pathlib.Path:
    try:
        details = path.lstat()
        identity = DpkgPackageResolver().resolve(str(path))
        valid = (
            path.is_absolute()
            and path.resolve(strict=True) == path
            and stat.S_ISREG(details.st_mode)
            and details.st_uid == 0
            and not details.st_mode & 0o022
            and (not executable or bool(details.st_mode & 0o111))
            and identity is not None
            and identity.name == package
        )
    except OSError:
        valid = False
    if not valid:
        raise AssertionError("release-host package prerequisite is not authenticated")
    return path


def test_engineer_compose_override_preserves_the_outer_boundary() -> None:
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    override = yaml.safe_load(_text("compose.override.yml"))
    backend = override["services"]["backend"]

    assert base["services"]["backend"]["pids_limit"] == 512
    assert base["services"]["telegram"]["pids_limit"] == 256
    assert backend == {
        "read_only": True,
        "pids_limit": 512,
        "security_opt": [
            "no-new-privileges:true",
            "seccomp=/etc/friday-engineer/seccomp.json",
            "apparmor=friday-engineer-backend",
        ],
        "cap_drop": ["ALL"],
    }
    rendered = repr(override).casefold()
    assert "unconfined" not in rendered
    assert "sys_admin" not in rendered
    assert "privileged" not in rendered
    assert "cap_add" not in rendered


def test_seccomp_is_default_deny_with_only_the_observed_bwrap_delta() -> None:
    profile = json.loads(_text("seccomp.json"))

    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultErrnoRet"] == 1
    assert {item["architecture"] for item in profile["archMap"]} == {
        "SCMP_ARCH_X86_64",
        "SCMP_ARCH_AARCH64",
    }
    baseline = json.dumps(profile["syscalls"][0]["names"], separators=(",", ":")).encode()
    assert len(profile["syscalls"][0]["names"]) == 361
    assert hashlib.sha256(baseline).hexdigest() == (
        "85fbce66b8dfa0db6bb2318d9a74b8829a8f777c2d2dcadf1cea08bcb108a7be"
    )

    setup_rule = next(
        rule
        for rule in profile["syscalls"]
        if set(rule["names"]) == {"mount", "umount2", "pivot_root", "sethostname"}
    )
    assert setup_rule["action"] == "SCMP_ACT_ALLOW"

    unshare = _seccomp_rules("unshare", action="SCMP_ACT_ALLOW")
    assert unshare == [
        {
            "names": ["unshare"],
            "action": "SCMP_ACT_ALLOW",
            "args": [
                {
                    "index": 0,
                    "value": 0xFFFFFFFF,
                    "valueTwo": 0x10000000,
                    "op": "SCMP_CMP_MASKED_EQ",
                }
            ],
            "comment": "Exact CLONE_NEWUSER used by bubblewrap --disable-userns",
        }
    ]

    clone = _seccomp_rules("clone", action="SCMP_ACT_ALLOW")
    assert len(clone) == 2
    exact_namespace = next(rule for rule in clone if rule["args"][0].get("valueTwo"))
    assert exact_namespace["args"] == [
        {
            "index": 0,
            "value": 0xFFFFFFFF,
            "valueTwo": 0x7E020011,
            "op": "SCMP_CMP_MASKED_EQ",
        }
    ]
    ordinary_clone = next(rule for rule in clone if not rule["args"][0].get("valueTwo"))
    assert ordinary_clone["args"] == [{"index": 0, "value": 0x7E020000, "op": "SCMP_CMP_MASKED_EQ"}]

    assert _seccomp_rules("clone3") == [
        {
            "names": ["clone3"],
            "action": "SCMP_ACT_ERRNO",
            "errnoRet": 38,
            "comment": "Preserve Moby's ENOSYS fallback; namespace-capable clone3 is not admitted",
        }
    ]
    for forbidden in (
        "bpf",
        "fsopen",
        "init_module",
        "keyctl",
        "mount_setattr",
        "move_mount",
        "open_by_handle_at",
        "reboot",
        "setns",
        "userfaultfd",
    ):
        assert not _seccomp_rules(forbidden, action="SCMP_ACT_ALLOW")


def test_apparmor_profile_is_enforcing_grammar_not_an_escape_hatch() -> None:
    profile = _text("apparmor/friday-engineer-backend")

    assert 'profile "friday-engineer-backend"' in profile
    assert "abi <abi/4.0>" in profile
    assert "  userns," in profile
    assert "  mount," in profile
    assert "  umount," in profile
    assert "  pivot_root," in profile
    assert "deny mount" not in profile
    assert "flags=(unconfined)" not in profile

    with pytest.raises(AssertionError, match="prerequisite is not authenticated"):
        _require_package_file(DEPLOY / "missing-apparmor-parser", "apparmor", executable=True)
    parser = _require_package_file(pathlib.Path("/usr/sbin/apparmor_parser"), "apparmor", executable=True)
    system_policy = _require_package_file(pathlib.Path("/etc/apparmor.d/bwrap-userns-restrict"), "apparmor")
    for policy in (system_policy, DEPLOY / "apparmor/friday-engineer-backend"):
        completed = subprocess.run(  # noqa: S603 - fixed parser and reviewed policies
            [str(parser), "-Q", "-K", "-T", str(policy)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr


def test_sandbox_limits_are_applied_by_trusted_prlimit_without_preexec() -> None:
    source = (ROOT / "friday/organs/engineer/sandbox.py").read_text(encoding="utf-8")
    argv = sandbox._limited_sandbox_argv(pathlib.Path("/tmp/work"))  # noqa: SLF001

    assert "preexec_fn" not in source
    assert argv[:7] == [
        "/usr/bin/prlimit",
        "--core=0:0",
        "--cpu=30:31",
        "--fsize=52428800:52428800",
        "--as=805306368:805306368",
        "--nofile=64:64",
        "--",
    ]
    assert argv[7] == "/usr/bin/bwrap"
    assert not any(item.startswith("--nproc=") for item in argv)
    assert "--unshare-all" in argv
    assert "--disable-userns" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"


def test_real_sandbox_smoke_proves_network_namespace_and_connectivity(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_SMOKE_SUCCESS_KEY", None)
    monkeypatch.setattr(sandbox, "_SMOKE_SUCCESS_RESULT", None)

    assert sandbox.smoke_preflight() == {
        "ok": True,
        "boundary": "bubblewrap",
        "network": "none",
        "protocol": 1,
        "network_namespace": "isolated",
        "external_interfaces": 0,
        "external_routes": 0,
        "ipv4_connectivity": "blocked",
        "ipv6_connectivity": "blocked",
    }


def test_sandbox_preflight_rejects_an_untrusted_prlimit(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "prlimit"
    candidate.write_text("not a trusted executable", encoding="ascii")
    candidate.chmod(0o777)
    monkeypatch.setattr(sandbox, "PRLIMIT", candidate)

    assert sandbox.preflight() == {"ok": False, "reason": "prlimit_unavailable"}


@pytest.mark.parametrize("limit", [None, sandbox.MAX_ADMITTED_CGROUP_PIDS + 1])
def test_sandbox_preflight_requires_a_finite_pid_cgroup(monkeypatch, limit) -> None:
    monkeypatch.setattr(sandbox, "_current_cgroup_pids_limit", lambda: limit)

    assert sandbox.preflight() == {"ok": False, "reason": "pid_cgroup_unbounded"}


def test_sandbox_resolves_the_current_unified_pid_cgroup_exactly(tmp_path, monkeypatch) -> None:
    cgroup_root = tmp_path / "cgroup"
    current = cgroup_root / "user.slice" / "friday.scope"
    current.mkdir(parents=True)
    (current / "pids.max").write_text("512\n", encoding="ascii")
    membership = tmp_path / "self.cgroup"
    membership.write_text("0::/user.slice/friday.scope\n", encoding="ascii")
    monkeypatch.setattr(sandbox, "CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(sandbox, "SELF_CGROUP", membership)

    assert sandbox._current_cgroup_pids_limit() == 512  # noqa: SLF001

    (current / "pids.max").write_text("max\n", encoding="ascii")
    assert sandbox._current_cgroup_pids_limit() is None  # noqa: SLF001


def test_image_and_operator_scripts_ship_the_complete_runtime_contract() -> None:
    dockerfile = (ROOT / "docker/Dockerfile.backend").read_text(encoding="utf-8")
    assert (
        "apt-get install -y --no-install-recommends bubblewrap python3-minimal util-linux "
        "nmap dnsutils file binutils openssl"
    ) in dockerfile
    for executable in (
        "/usr/bin/bwrap",
        "/usr/bin/python3",
        "/usr/bin/prlimit",
        "/usr/bin/nmap",
        "/usr/bin/dig",
        "/usr/bin/host",
        "/usr/bin/file",
        "/usr/bin/strings",
        "/usr/bin/readelf",
        "/usr/bin/objdump",
        "/usr/bin/openssl",
    ):
        assert executable in dockerfile

    for relative in ("install-apparmor.sh", "uninstall-apparmor.sh", "verify-runtime.sh"):
        script = DEPLOY / relative
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\nset -eu\n")
        completed = subprocess.run(  # noqa: S603 - fixed shell syntax check
            ["/bin/sh", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == 0, completed.stderr

    installer = _text("install-apparmor.sh")
    uninstaller = _text("uninstall-apparmor.sh")
    for policy_script in (installer, uninstaller):
        assert "SECCOMP_TARGET=$SECCOMP_DIRECTORY/seccomp.json" in policy_script
        assert '/usr/bin/cmp -s -- "$SECCOMP_SOURCE" "$SECCOMP_TARGET"' in policy_script
    assert '/usr/bin/install -o root -g root -m 0644 "$SECCOMP_SOURCE"' in installer
    assert '/usr/bin/stat -c %u:%g:%a:%h -- "$SECCOMP_TARGET"' in installer
    assert installer.index("SECCOMP_CREATED=1", installer.index("else\n    TEMP_PATH=")) < installer.index(
        '/usr/bin/mv -f -- "$TEMP_PATH" "$SECCOMP_TARGET"'
    )
    assert installer.rindex("PROFILE_CREATED=1") < installer.index(
        '/usr/bin/mv -f -- "$TEMP_PATH" "$PROFILE_TARGET"'
    )

    smoke = _text("verify-runtime.sh")
    assert "docker info --format" in smoke
    assert "rootless Docker is outside" in smoke
    assert "{{.AppArmorProfile}}" in smoke
    assert "{{.HostConfig.ReadonlyRootfs}}" in smoke
    assert "{{.HostConfig.Privileged}}" in smoke
    assert "{{.HostConfig.PidsLimit}}" in smoke
    assert "{{json .HostConfig.CapDrop}}" in smoke
    assert "{{json .HostConfig.CapAdd}}" in smoke
    assert "/usr/bin/id -u" in smoke
    assert '/usr/bin/cmp -s -- "$PROFILE_SOURCE" "$PROFILE_TARGET"' in smoke
    assert "SECCOMP_SOURCE=$SCRIPT_DIR/seccomp.json" in smoke
    assert "SECCOMP_TARGET=/etc/friday-engineer/seccomp.json" in smoke
    assert '[ "$SECCOMP_SELECTED" = "$SECCOMP_TARGET" ]' in smoke
    assert 'stat -c %u:%g:%a:%h -- "$SECCOMP_SELECTED"' in smoke
    assert '/usr/bin/cmp -s -- "$SECCOMP_SOURCE" "$SECCOMP_SELECTED"' in smoke
    assert "NoNewPrivs:" in smoke
    assert "Seccomp:" in smoke
    assert "smoke_preflight" in smoke
    assert 'r.get("network_namespace") == "isolated"' in smoke
    assert 'r.get("external_routes") == 0' in smoke
    assert 'r.get("ipv4_connectivity") == "blocked"' in smoke
    assert 'r.get("ipv6_connectivity") == "blocked"' in smoke
    assert "seccomp=unconfined" in smoke

    readme = _text("README.md")
    assert "CAP_SYS_ADMIN" in readme
    assert "apparmor=unconfined" in readme
    assert "seccomp=unconfined" in readme
    assert "verify-runtime.sh" in readme
    assert "`capa`, `rabin2`, and `apkid` remain" in readme
