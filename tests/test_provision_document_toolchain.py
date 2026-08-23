"""Offline contracts for the explicit rootless document-toolchain provisioner."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tools.provision_document_toolchain as provisioner

HOST = provisioner.HostIdentity(
    os_id="ubuntu",
    version_id="26.04",
    architecture="amd64",
    multiarch="x86_64-linux-gnu",
    dpkg_status_sha256="a" * 64,
)
VERSIONS = {
    "tesseract-ocr": "5.5.0-1build1",
    "tesseract-ocr-rus": "1:4.1.0-2build1",
    "libreoffice-core-nogui": "4:26.2.5.2-0ubuntu0.26.04.1",
    "libreoffice-writer-nogui": "4:26.2.5.2-0ubuntu0.26.04.1",
    "libreoffice-calc-nogui": "4:26.2.5.2-0ubuntu0.26.04.1",
    "libreoffice-impress-nogui": "4:26.2.5.2-0ubuntu0.26.04.1",
}
ARCHITECTURES = {
    "tesseract-ocr-rus": "all",
}
TRUSTED_SOURCE = "http://archive.ubuntu.com/ubuntu resolute/universe amd64 Packages"


def _package_payload(package: str) -> bytes:
    return f"signed synthetic deb: {package}\n".encode()


def _record(package: str) -> provisioner.PackageRecord:
    payload = _package_payload(package)
    architecture = ARCHITECTURES.get(package, "amd64")
    return provisioner.PackageRecord(
        package=package,
        version=VERSIONS[package],
        architecture=architecture,
        filename=f"pool/universe/s/synthetic/{package}.deb",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        apt_source=TRUSTED_SOURCE,
    )


def _plan() -> provisioner.ToolchainPlan:
    return provisioner._make_plan(  # noqa: SLF001 - exact operator contract
        HOST,
        tuple(_record(package) for package in sorted(VERSIONS)),
    )


def _solver_output() -> bytes:
    lines = []
    for package in reversed(sorted(VERSIONS)):
        version = VERSIONS[package]
        architecture = ARCHITECTURES.get(package, "amd64")
        lines.append(f"Inst {package} ({version} Ubuntu:26.04/resolute [${architecture}])")
    return "\n".join(lines).replace("[$", "[").encode()


def _metadata(record: provisioner.PackageRecord, *, origin: str = "Ubuntu") -> bytes:
    return (
        f"Package: {record.package}\n"
        f"Architecture: {record.architecture}\n"
        f"Version: {record.version}\n"
        f"Filename: {record.filename}\n"
        f"Size: {record.size}\n"
        f"SHA256: {record.sha256}\n"
        f"Origin: {origin}\n\n"
    ).encode()


class PlanningRunner:
    def __init__(self, *, origin: str = "Ubuntu", source: str = TRUSTED_SOURCE) -> None:
        self.origin = origin
        self.source = source
        self.capture_calls: list[tuple[str, ...]] = []
        self.quiet_calls: list[tuple[str, ...]] = []

    def capture(
        self,
        command: Any,
        *,
        failure_code: str,
        timeout: float,
        max_output_bytes: int = provisioner.MAX_COMMAND_OUTPUT_BYTES,
    ) -> bytes:
        del failure_code, timeout, max_output_bytes
        normalized = tuple(command)
        self.capture_calls.append(normalized)
        if normalized[0] == provisioner.APT_GET:
            if "indextargets" in normalized:
                return (
                    b"Packages\thttp://archive.ubuntu.com/ubuntu\tresolute\tuniverse\t"
                    b"amd64\tUbuntu\tUbuntu\n"
                )
            return _solver_output()
        if normalized[0] == provisioner.APT_CACHE:
            spec = normalized[-1]
            package = spec.split(":", 1)[0]
            return _metadata(_record(package), origin=self.origin)
        if normalized[0] == provisioner.APT:
            return f"Package: synthetic\nAPT-Sources: {self.source}\n".encode()
        raise AssertionError(normalized)

    def quiet(
        self,
        command: Any,
        *,
        failure_code: str,
        timeout: float,
        cwd: Path | None = None,
        environment: Any = None,
    ) -> None:
        del failure_code, timeout, cwd, environment
        self.quiet_calls.append(tuple(command))


class InstallingRunner:
    def __init__(self, plan: provisioner.ToolchainPlan) -> None:
        self.plan = plan
        self.quiet_calls: list[tuple[str, ...]] = []
        self.identities: dict[str, provisioner.PackageRecord] = {}

    def capture(
        self,
        command: Any,
        *,
        failure_code: str,
        timeout: float,
        max_output_bytes: int = provisioner.MAX_COMMAND_OUTPUT_BYTES,
    ) -> bytes:
        del failure_code, timeout, max_output_bytes
        normalized = tuple(command)
        if normalized[0] != provisioner.DPKG_DEB or "--show" not in normalized:
            raise AssertionError(normalized)
        record = self.identities[Path(normalized[-1]).name]
        return f"{record.package}\t{record.version}\t{record.architecture}\n".encode()

    def quiet(
        self,
        command: Any,
        *,
        failure_code: str,
        timeout: float,
        cwd: Path | None = None,
        environment: Any = None,
    ) -> None:
        del failure_code, timeout, environment
        normalized = tuple(command)
        self.quiet_calls.append(normalized)
        if normalized[0] == provisioner.APT_GET and "download" in normalized:
            assert cwd is not None
            for index, record in enumerate(self.plan.packages):
                name = f"package-{index}.deb"
                (cwd / name).write_bytes(_package_payload(record.package))
                self.identities[name] = record
            return
        if normalized[:2] == (provisioner.DPKG_DEB, "--extract"):
            rootfs = Path(normalized[-1])
            required = {
                "usr/bin/tesseract": b"tesseract",
                "usr/lib/libreoffice/program/soffice": b"soffice",
                "usr/share/tesseract-ocr/5/tessdata/rus.traineddata": b"rus",
                "usr/share/tesseract-ocr/5/tessdata/eng.traineddata": b"eng",
                "usr/share/libreoffice/registry/main.xcd": b"registry",
                "etc/libreoffice/sofficerc": b"configuration",
            }
            for relative, payload in required.items():
                path = rootfs / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                if path.name in {"tesseract", "soffice"}:
                    path.chmod(0o755)
            return
        raise AssertionError(normalized)


def test_audit_is_deterministic_and_never_invokes_a_download() -> None:
    runner = PlanningRunner()

    first = provisioner.build_plan(runner, host=HOST)
    second = provisioner.build_plan(runner, host=HOST)
    receipt = provisioner.audit_receipt(first)

    assert first.manifest_bytes == second.manifest_bytes
    assert first.plan_sha256 == second.plan_sha256
    assert receipt["install_requires_confirmation"] is True
    assert receipt["manifest"] == first.manifest
    assert runner.quiet_calls == []
    assert all("download" not in call for call in runner.capture_calls)
    assert [record.package for record in first.packages] == sorted(VERSIONS)


def test_only_ubuntu_origin_metadata_is_accepted() -> None:
    with pytest.raises(provisioner.ProvisionFailure, match="package_metadata_untrusted"):
        provisioner.build_plan(PlanningRunner(origin="ThirdParty"), host=HOST)

    with pytest.raises(provisioner.ProvisionFailure, match="package_source_untrusted"):
        provisioner.build_plan(
            PlanningRunner(
                source="https://third-party.test/repo stable/main amd64 Packages",
            ),
            host=HOST,
        )


def test_solver_must_return_every_requested_root_and_no_removal() -> None:
    incomplete = _solver_output().replace(b"Inst tesseract-ocr ", b"Skip tesseract-ocr ")
    with pytest.raises(provisioner.ProvisionFailure, match="apt_solver_closure_incomplete"):
        provisioner._parse_install_set(  # noqa: SLF001
            incomplete,
            host_architecture="amd64",
        )
    with pytest.raises(provisioner.ProvisionFailure, match="apt_solver_unsafe"):
        provisioner._parse_install_set(  # noqa: SLF001
            _solver_output() + b"\nRemv libc6 [1.0]",
            host_architecture="amd64",
        )


def test_wrappers_bind_all_runtime_state_without_an_env_file() -> None:
    tesseract = provisioner._tesseract_wrapper(HOST.multiarch).decode()  # noqa: SLF001
    office = provisioner._libreoffice_wrapper(HOST.multiarch).decode()  # noqa: SLF001

    assert 'TESSDATA_PREFIX="$root/rootfs/usr/share/tesseract-ocr/5/tessdata"' in tesseract
    assert 'exec "$root/rootfs/usr/bin/tesseract" "$@"' in tesseract
    assert "FRIDAY_" not in tesseract
    assert "--unshare-all" in office and "--clearenv" in office
    assert '--bind "$TMPDIR" "$TMPDIR"' in office
    assert "--ro-bind /usr /host/usr" in office
    assert "--ro-bind \"$root/rootfs/usr/lib/libreoffice\" /usr/lib/libreoffice" in office
    assert "--setenv LD_LIBRARY_PATH /opt/friday-document-toolchain/" in office
    assert "-- /usr/lib/libreoffice/program/soffice \"$@\"" in office
    assert ".env" not in office


@pytest.mark.skipif(not Path(provisioner.BWRAP).is_file(), reason="bubblewrap is host contract")
def test_libreoffice_wrapper_has_a_real_clean_namespace_and_writable_workdir(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "toolchain"
    wrapper = toolchain / "bin/libreoffice"
    soffice = toolchain / "rootfs/usr/lib/libreoffice/program/soffice"
    for directory in (
        wrapper.parent,
        soffice.parent,
        toolchain / "rootfs/usr/share/libreoffice",
        toolchain / "rootfs/etc/libreoffice",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes(provisioner._libreoffice_wrapper(HOST.multiarch))  # noqa: SLF001
    wrapper.chmod(0o500)
    soffice.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "[ -z \"${LEAK_ME+x}\" ]\n"
        "[ -d /usr/lib/libreoffice ]\n"
        "[ -d /usr/share/libreoffice ]\n"
        "[ -d /etc/libreoffice ]\n"
        "[ -d /opt/friday-document-toolchain ]\n"
        "[ -w \"$TMPDIR\" ]\n"
        "printf clear > \"$TMPDIR/wrapper-canary\"\n",
        encoding="ascii",
    )
    soffice.chmod(0o500)

    with tempfile.TemporaryDirectory(prefix="friday-office-", dir="/tmp") as temporary:
        work = Path(temporary)
        work.chmod(0o700)
        completed = subprocess.run(
            (str(wrapper), "--headless"),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={"HOME": str(work), "TMPDIR": str(work), "LEAK_ME": "must-not-cross"},
            timeout=10,
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        assert (work / "wrapper-canary").read_text(encoding="ascii") == "clear"

        (work / "wrapper-canary").unlink()
        work.chmod(0o750)
        rejected = subprocess.run(
            (str(wrapper), "--headless"),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={"HOME": str(work), "TMPDIR": str(work)},
            timeout=10,
        )
        assert rejected.returncode == 64
        assert not (work / "wrapper-canary").exists()


def test_install_requires_the_exact_audited_id_before_touching_disk(tmp_path: Path) -> None:
    plan = _plan()
    runner = InstallingRunner(plan)
    base = tmp_path / "toolchains"
    activation = tmp_path / "bin"

    with pytest.raises(provisioner.ProvisionFailure, match="installation_confirmation_mismatch"):
        provisioner.install_plan(
            plan,
            confirmation="wrong",
            runner=runner,
            base_dir=base,
            activation_dir=activation,
            perform_preflight=False,
        )

    assert not base.exists()
    assert not activation.exists()
    assert runner.quiet_calls == []


def test_install_seals_one_version_and_activates_only_two_atomic_links(tmp_path: Path) -> None:
    plan = _plan()
    runner = InstallingRunner(plan)
    base = tmp_path / "toolchains"
    activation = tmp_path / "bin"

    receipt = provisioner.install_plan(
        plan,
        confirmation=plan.toolchain_id,
        runner=runner,
        base_dir=base,
        activation_dir=activation,
        perform_preflight=False,
        perform_canary=False,
        verify_host_state=False,
    )

    final = base / plan.toolchain_id
    assert receipt["status"] == "installed"
    assert stat.S_IMODE(final.stat().st_mode) == 0o500
    assert stat.S_IMODE((final / "manifest.json").stat().st_mode) == 0o400
    assert stat.S_IMODE((final / "bin/tesseract").stat().st_mode) == 0o500
    assert not any(path.name.startswith(".staging-") for path in base.iterdir())
    for name in provisioner.COMMAND_NAMES:
        link = activation / name
        assert link.is_symlink()
        assert link.resolve(strict=True) == (final / "bin" / name).resolve(strict=True)
    assert not list(activation.glob(".friday-*.new"))
    assert not list(activation.glob(".friday-*.rollback"))

    calls_before = len(runner.quiet_calls)
    provisioner.install_plan(
        plan,
        confirmation=plan.toolchain_id,
        runner=runner,
        base_dir=base,
        activation_dir=activation,
        perform_preflight=False,
        perform_canary=False,
        verify_host_state=False,
    )
    assert len(runner.quiet_calls) == calls_before


def test_an_existing_regular_command_blocks_before_download(tmp_path: Path) -> None:
    plan = _plan()
    runner = InstallingRunner(plan)
    base = tmp_path / "toolchains"
    activation = tmp_path / "bin"
    base.mkdir(mode=0o700)
    activation.mkdir(mode=0o700)
    (activation / "tesseract").write_text("foreign command", encoding="utf-8")

    with pytest.raises(provisioner.ProvisionFailure, match="activation_link_untrusted"):
        provisioner.install_plan(
            plan,
            confirmation=plan.toolchain_id,
            runner=runner,
            base_dir=base,
            activation_dir=activation,
            perform_preflight=False,
            verify_host_state=False,
        )

    assert runner.quiet_calls == []
    assert (activation / "tesseract").read_text(encoding="utf-8") == "foreign command"


def test_two_command_activation_rolls_back_if_the_second_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "toolchains"
    final = base / "version"
    activation = tmp_path / "bin"
    (final / "bin").mkdir(parents=True)
    activation.mkdir()
    activation.chmod(0o700)
    for name in provisioner.COMMAND_NAMES:
        target = final / "bin" / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        target.chmod(0o500)
    original = provisioner._write_atomic_symlink  # noqa: SLF001
    calls = 0

    def fail_second(link: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise provisioner.ProvisionFailure("activation_link_replace_failed")
        original(link, target)

    monkeypatch.setattr(provisioner, "_write_atomic_symlink", fail_second)

    with pytest.raises(provisioner.ProvisionFailure, match="activation_failed"):
        provisioner._activate(  # noqa: SLF001
            final,
            base=base,
            activation_dir=activation,
            uid=os.geteuid(),
        )

    assert not (activation / "tesseract").exists()
    assert not (activation / "libreoffice").exists()


def test_downloaded_deb_must_match_signed_index_size_and_sha256(tmp_path: Path) -> None:
    plan = _plan()
    runner = InstallingRunner(plan)
    downloads = tmp_path / "downloads"
    original_quiet = runner.quiet

    def corrupting_quiet(*args: Any, **kwargs: Any) -> None:
        original_quiet(*args, **kwargs)
        command = tuple(args[0])
        if command[0] == provisioner.APT_GET:
            first = next(downloads.iterdir())
            first.write_bytes(first.read_bytes() + b"tampered")

    runner.quiet = corrupting_quiet  # type: ignore[method-assign]

    with pytest.raises(provisioner.ProvisionFailure, match="package_digest_mismatch"):
        provisioner._download_and_verify(plan, downloads, runner)  # noqa: SLF001


def test_failure_receipt_never_echoes_exception_or_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "owner-private-token-should-never-appear"
    monkeypatch.setenv("FRIDAY_LLM_API_KEY", secret)

    def fail(_runner: Any) -> provisioner.ToolchainPlan:
        raise RuntimeError(secret)

    monkeypatch.setattr(provisioner, "build_plan", fail)

    assert provisioner.main(("audit",)) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["failure_codes"] == ["unexpected_failure"]
    assert secret not in captured.out + captured.err


def test_private_directories_reject_group_or_world_writes(tmp_path: Path) -> None:
    path = tmp_path / "shared"
    path.mkdir(mode=0o777)
    path.chmod(0o777)

    with pytest.raises(provisioner.ProvisionFailure, match="toolchain_base_untrusted"):
        provisioner._private_base(path, uid=os.geteuid())  # noqa: SLF001
    with pytest.raises(provisioner.ProvisionFailure, match="activation_directory_untrusted"):
        provisioner._activation_directory(path, uid=os.geteuid())  # noqa: SLF001


def test_sealing_rejects_links_to_private_host_state(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    (safe_root / "usr").mkdir()
    (safe_root / "usr/link").symlink_to("/usr/lib/libsafe.so")
    provisioner._freeze_tree(safe_root, uid=os.geteuid())  # noqa: SLF001

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir()
    (unsafe_root / "escape").symlink_to("/home/owner/private-payload")
    with pytest.raises(provisioner.ProvisionFailure, match="toolchain_symlink_invalid"):
        provisioner._freeze_tree(unsafe_root, uid=os.geteuid())  # noqa: SLF001
