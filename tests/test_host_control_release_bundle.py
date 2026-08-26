"""Closed deterministic release contracts for the Host Control bundle."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tools import build_host_control_release_bundle as bundle

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.3"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed test Git argv
        [bundle.GIT, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _package_payloads(version: str) -> dict[str, bytes]:
    return {
        "friday/__init__.py": f'"""Friday."""\n\n__version__ = "{version}"\n'.encode(),
        "friday/host_control/__init__.py": b'"""host control"""\n',
        "friday_host_agent/__main__.py": b"def main(): return 0\n",
        "friday_host_agent/daemon.py": b"DAEMON = True\n",
        "friday_package_broker/__main__.py": b"def main(): return 0\n",
        "friday_package_broker/approval.py": b"APPROVAL = True\n",
        "friday_package_broker/daemon.py": b"DAEMON = True\n",
    }


def _write_wheel(path: Path, *, metadata_version: str = VERSION) -> Path:
    dist_info = f"friday-{metadata_version}.dist-info"
    payloads = {
        **_package_payloads(metadata_version),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.4\nName: friday\nVersion: {metadata_version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": (
            b"[console_scripts]\n"
            b"friday-host-agent = friday_host_agent.__main__:main\n"
            b"friday-package-broker = friday_package_broker.__main__:main\n"
        ),
        f"{dist_info}/licenses/LICENSE": b"Friday synthetic test license.\n" * 16,
    }
    record_name = f"{dist_info}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in payloads.items():
        writer.writerow((name, _record_hash(data), str(len(data))))
    writer.writerow((record_name, "", ""))
    payloads[record_name] = record.getvalue().encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as wheel:
        for name, data in payloads.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            wheel.writestr(info, data)
    return path


def _source_repo(tmp_path: Path, *, version: str = VERSION) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        f'[project]\nname = "friday"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    for relative, data in _package_payloads(version).items():
        package_file = source / relative
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_bytes(data)
    deploy = source / "deploy" / "host-control"
    for relative in bundle.DEPLOY_FILES:
        destination = deploy / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "deploy" / "host-control" / relative, destination)
        destination.chmod(0o755 if relative in bundle.EXECUTABLE_DEPLOY_FILES else 0o644)

    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "release-test@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "exact release source")
    assert len(_git(source, "rev-parse", "HEAD")) == 40
    return source


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = _source_repo(tmp_path)
    wheel = _write_wheel(tmp_path / "artifacts" / f"friday-{VERSION}-py3-none-any.whl")
    return source, wheel


def _output(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"friday-host-control-{VERSION}.tar.gz"


def _build(tmp_path: Path) -> tuple[Path, bundle.BundleReceipt]:
    source, wheel = _inputs(tmp_path)
    output = _output(tmp_path / "release")
    receipt = bundle.build_bundle(source_root=source, wheel=wheel, output=output)
    return output, receipt


def _members(path: Path) -> list[tarfile.TarInfo]:
    data = bundle._decompress_archive(path.read_bytes())  # noqa: SLF001
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return archive.getmembers()


def _rewrite_archive(
    source: Path,
    destination: Path,
    transform: Callable[[tarfile.TarInfo, bytes], tuple[tarfile.TarInfo, bytes]],
) -> str:
    tar_data = bundle._decompress_archive(source.read_bytes())  # noqa: SLF001
    rewritten = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:") as original,
        tarfile.open(fileobj=rewritten, mode="w", format=tarfile.USTAR_FORMAT) as output,
    ):
        for member in original.getmembers():
            handle = original.extractfile(member)
            data = handle.read() if handle is not None else b""
            changed, payload = transform(member, data)
            changed.size = len(payload) if changed.isreg() else 0
            output.addfile(changed, io.BytesIO(payload) if changed.isreg() else None)
    encoded = bundle._gzip_bytes(rewritten.getvalue())  # noqa: SLF001
    destination.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def test_build_is_byte_deterministic_and_manifest_covers_the_closed_bundle(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    first = _output(tmp_path / "first")
    second = _output(tmp_path / "second")

    first_receipt = bundle.build_bundle(source_root=source, wheel=wheel, output=first)
    second_receipt = bundle.build_bundle(source_root=source, wheel=wheel, output=second)

    assert first.read_bytes() == second.read_bytes()
    assert first_receipt.archive_sha256 == second_receipt.archive_sha256
    assert Path(f"{first}.sha256").read_text(encoding="ascii") == (
        f"{first_receipt.archive_sha256}  {first.name}\n"
    )
    verified = bundle.verify_bundle(archive=first, expected_sha256=first_receipt.archive_sha256)
    assert verified == first_receipt

    tar_data = bundle._decompress_archive(first.read_bytes())  # noqa: SLF001
    manifest_data, payloads = bundle._archive_payloads(tar_data)  # noqa: SLF001
    manifest = json.loads(manifest_data)
    assert set(manifest) == {"schema", "source_commit", "version", "wheel", "deploy"}
    assert manifest["schema"] == bundle.SCHEMA
    assert manifest["source_commit"] == _git(source, "rev-parse", "HEAD")
    assert manifest["version"] == VERSION
    assert manifest["wheel"] == {
        "name": wheel.name,
        "path": f"wheel/{wheel.name}",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "size": wheel.stat().st_size,
    }
    assert [item["path"] for item in manifest["deploy"]] == [
        f"deploy/host-control/{name}" for name in bundle.DEPLOY_FILES
    ]
    assert list(payloads) == [
        bundle.MANIFEST_NAME,
        f"wheel/{wheel.name}",
        *(f"deploy/host-control/{name}" for name in bundle.DEPLOY_FILES),
    ]

    members = _members(first)
    assert all(member.type == tarfile.REGTYPE and member.isreg() for member in members)
    assert all(member.uid == member.gid == member.mtime == 0 for member in members)
    assert all(member.uname == member.gname == member.linkname == "" for member in members)
    expected_modes = [
        0o644,
        0o644,
        *(0o755 if relative in bundle.EXECUTABLE_DEPLOY_FILES else 0o644 for relative in bundle.DEPLOY_FILES),
    ]
    assert [member.mode for member in members] == expected_modes


def test_cli_verify_requires_the_external_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive, receipt = _build(tmp_path)

    assert (
        bundle.main(
            [
                "verify",
                "--archive",
                str(archive),
                "--expected-sha256",
                receipt.archive_sha256,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["archive_sha256"] == receipt.archive_sha256


def test_build_rejects_a_dirty_source_tree(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    with (source / "deploy/host-control/README.md").open("ab") as handle:
        handle.write(b"dirty\n")

    with pytest.raises(bundle.BundleError, match="dirty"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_a_git_replacement_ref_that_falsely_makes_the_tree_clean(
    tmp_path: Path,
) -> None:
    source, wheel = _inputs(tmp_path)
    original_commit = _git(source, "rev-parse", "HEAD")
    readme = source / "deploy/host-control/README.md"
    readme.write_bytes(readme.read_bytes() + b"replacement payload\n")
    _git(source, "add", "deploy/host-control/README.md")
    replacement_tree = _git(source, "write-tree")
    replacement_commit = _git(
        source,
        "commit-tree",
        replacement_tree,
        "-m",
        "untrusted replacement release tree",
    )
    _git(source, "replace", original_commit, replacement_commit)

    # Ordinary Git now reports a clean replacement tree while rev-parse still
    # names the original commit.  A release attester must reject that false
    # attribution instead of archiving replacement bytes under original_commit.
    assert replacement_commit != original_commit
    assert _git(source, "rev-parse", "HEAD") == original_commit
    assert _git(source, "status", "--porcelain=v1") == ""
    assert _git(source, "cat-file", "blob", "HEAD:deploy/host-control/README.md").endswith(
        "replacement payload"
    )

    with pytest.raises(bundle.BundleError, match="replacement refs"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_pycache_even_when_git_ignores_it(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    cache = source / "deploy/host-control/__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"cache")
    (source / ".git/info/exclude").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")

    with pytest.raises(bundle.BundleError, match="cache artifacts"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_an_extra_closed_deploy_member(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    (source / "deploy/host-control/unreviewed.sh").write_text("#!/bin/sh\n", encoding="ascii")

    with pytest.raises(bundle.BundleError, match="closed release file set"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_a_tracked_deploy_symlink(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    readme = source / "deploy/host-control/README.md"
    readme.unlink()
    readme.symlink_to("install.sh")
    _git(source, "add", "deploy/host-control/README.md")
    _git(source, "commit", "--quiet", "-m", "replace deploy file with symlink")

    with pytest.raises(bundle.BundleError, match="symbolic link|invalid type or mode"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


@pytest.mark.parametrize(
    ("relative", "mode"),
    [("README.md", 0o755), ("install.sh", 0o644)],
)
def test_build_rejects_a_wrong_committed_deploy_mode(
    tmp_path: Path,
    relative: str,
    mode: int,
) -> None:
    source, wheel = _inputs(tmp_path)
    deploy_file = source / "deploy/host-control" / relative
    deploy_file.chmod(mode)
    _git(source, "add", f"deploy/host-control/{relative}")
    _git(source, "commit", "--quiet", "-m", "drift deploy file mode")

    with pytest.raises(bundle.BundleError, match="invalid type or mode"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_symlinked_wheel_and_output_inside_source(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    linked = tmp_path / "artifacts" / "linked.whl"
    linked.symlink_to(wheel)
    with pytest.raises(bundle.BundleError, match="canonical regular"):
        bundle.build_bundle(source_root=source, wheel=linked, output=_output(tmp_path / "release"))

    unsafe_output = source / f"friday-host-control-{VERSION}.tar.gz"
    with pytest.raises(bundle.BundleError, match="overlaps trusted release inputs"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=unsafe_output)


@pytest.mark.parametrize("source_version", ["1.2", "01.2.3", "1.2.3-rc1"])
def test_build_rejects_an_invalid_source_version(tmp_path: Path, source_version: str) -> None:
    source = _source_repo(tmp_path, version=source_version)
    wheel = _write_wheel(tmp_path / "artifacts" / f"friday-{VERSION}-py3-none-any.whl")

    with pytest.raises(bundle.BundleError, match="release identity"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_wheel_metadata_version_drift(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    wheel = _write_wheel(
        tmp_path / "artifacts" / f"friday-{VERSION}-py3-none-any.whl",
        metadata_version="1.2.4",
    )

    with pytest.raises(bundle.BundleError, match="distribution identity|metadata identity"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_wheel_package_bytes_from_another_commit(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    with zipfile.ZipFile(wheel, "r") as current:
        payloads = {info.filename: current.read(info) for info in current.infolist()}
    payloads["friday_host_agent/daemon.py"] = b"DAEMON = 'foreign build'\n"
    record_name = f"friday-{VERSION}.dist-info/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in payloads.items():
        if name != record_name:
            writer.writerow((name, _record_hash(data), str(len(data))))
    writer.writerow((record_name, "", ""))
    payloads[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as foreign:
        for name, data in payloads.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            foreign.writestr(info, data)

    with pytest.raises(bundle.BundleError, match="package bytes"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_wheel_missing_a_tracked_package_source(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    (source / "friday_host_agent/extra.py").write_text("EXTRA = True\n", encoding="ascii")
    _git(source, "add", "friday_host_agent/extra.py")
    _git(source, "commit", "--quiet", "-m", "add tracked package source")

    with pytest.raises(bundle.BundleError, match="package inventory"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_build_rejects_a_tracked_package_symlink(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    (source / "friday_host_agent/linked.py").symlink_to("daemon.py")
    _git(source, "add", "friday_host_agent/linked.py")
    _git(source, "commit", "--quiet", "-m", "add tracked package symlink")

    with pytest.raises(bundle.BundleError, match="tracked regular blob"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=_output(tmp_path / "release"))


def test_verify_rejects_wrong_external_digest(tmp_path: Path) -> None:
    archive, _receipt = _build(tmp_path)

    with pytest.raises(bundle.BundleError, match="external expected SHA-256"):
        bundle.verify_bundle(archive=archive, expected_sha256="0" * 64)


def test_verify_rejects_tamper_even_with_a_recomputed_external_digest(tmp_path: Path) -> None:
    archive, _receipt = _build(tmp_path)
    tampered = _output(tmp_path / "tampered")

    def mutate(member: tarfile.TarInfo, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
        if member.name == "deploy/host-control/README.md":
            data += b"tampered\n"
        return member, data

    digest = _rewrite_archive(archive, tampered, mutate)
    with pytest.raises(bundle.BundleError, match="deploy bytes"):
        bundle.verify_bundle(archive=tampered, expected_sha256=digest)


@pytest.mark.parametrize("variant", ["traversal", "symlink"])
def test_verify_rejects_unsafe_archive_members(tmp_path: Path, variant: str) -> None:
    archive, _receipt = _build(tmp_path)
    unsafe = _output(tmp_path / variant)

    def mutate(member: tarfile.TarInfo, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
        if member.name == "deploy/host-control/README.md":
            if variant == "traversal":
                member.name = "../escape"
            else:
                member.type = tarfile.SYMTYPE
                member.linkname = "manifest.json"
                data = b""
        return member, data

    digest = _rewrite_archive(archive, unsafe, mutate)
    with pytest.raises(bundle.BundleError, match="unsafe member path|member metadata"):
        bundle.verify_bundle(archive=unsafe, expected_sha256=digest)


def test_verify_rejects_noncanonical_member_mode(tmp_path: Path) -> None:
    archive, _receipt = _build(tmp_path)
    unsafe = _output(tmp_path / "mode")

    def mutate(member: tarfile.TarInfo, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
        if member.name == "deploy/host-control/install.sh":
            member.mode = 0o777
        return member, data

    digest = _rewrite_archive(archive, unsafe, mutate)
    with pytest.raises(bundle.BundleError, match="not canonical"):
        bundle.verify_bundle(archive=unsafe, expected_sha256=digest)


def test_build_refuses_to_overwrite_archive_or_sidecar(tmp_path: Path) -> None:
    source, wheel = _inputs(tmp_path)
    output = _output(tmp_path / "release")
    output.write_bytes(b"existing")

    with pytest.raises(bundle.BundleError, match="must not already exist"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=output)

    output.unlink()
    Path(f"{output}.sha256").write_text("existing\n", encoding="ascii")
    with pytest.raises(bundle.BundleError, match="must not already exist"):
        bundle.build_bundle(source_root=source, wheel=wheel, output=output)
