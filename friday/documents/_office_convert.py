"""Closed, bounded LibreOffice fallback for legacy binary Office files.

LibreOffice cannot consume an upload through stdin, so this is the one document
parser that uses disk.  It receives a private, mode-0700 temporary root, a fixed
basename, no inherited environment, and one caller-owned deadline.  The root is
created directly below ``/tmp`` because Friday's sandbox wrapper admits exactly
that host path; an ambient ``TMPDIR`` must never alter this security boundary.
Only the declared source/target pairs are accepted; the converted container is
parsed by Friday's existing bounded OOXML/ODG readers before any text is trusted.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from friday.config import env as config_env

_DEFAULT_CONVERSION_TIMEOUT_SEC = 45.0
_MAX_CONVERSION_INPUT_BYTES = 64 * 1024 * 1024
_MAX_CONVERSION_OUTPUT_BYTES = 128 * 1024 * 1024
_OFFICE_TEMP_PARENT = "/tmp"  # nosec B108 - fixed sandbox-wrapper contract
_TARGET_BY_SOURCE = {
    "doc": "docx",
    "dot": "docx",
    "abw": "docx",
    "hwp": "docx",
    "lwp": "docx",
    "psw": "docx",
    "sdw": "docx",
    "stw": "docx",
    "sxw": "docx",
    "wri": "docx",
    "zabw": "docx",
    # Microsoft Works/Mac Works `.wps` is intentionally not forced into the
    # Writer or Calc family.  Both services have registered import filters;
    # their common, parser-bounded carrier is PDF.
    "wps": "pdf",
    "wpt": "docx",
    "wpd": "docx",
    "pages": "docx",
    "xls": "xlsx",
    "xlsb": "xlsx",
    "xlt": "xlsx",
    "et": "xlsx",
    "ett": "xlsx",
    "numbers": "xlsx",
    "123": "xlsx",
    "dif": "xlsx",
    "gnm": "xlsx",
    "gnumeric": "xlsx",
    "mp": "xlsx",
    "stc": "xlsx",
    "sxc": "xlsx",
    "wb1": "xlsx",
    "wb2": "xlsx",
    "wdb": "xlsx",
    "wk1": "xlsx",
    "wk3": "xlsx",
    "wk4": "xlsx",
    "wks": "xlsx",
    "wq1": "xlsx",
    "wq2": "xlsx",
    "xlc": "xlsx",
    "xlk": "xlsx",
    "xlm": "xlsx",
    "xlw": "xlsx",
    "ppt": "pptx",
    "pot": "pptx",
    "pps": "pptx",
    "dpt": "pptx",
    "dps": "pptx",
    "key": "pptx",
    "sdd": "pptx",
    "sti": "pptx",
    "sxi": "pptx",
    "pub": "odg",
    "vdx": "odg",
    "vsd": "odg",
    "vsdm": "odg",
    "vsdx": "odg",
    "vstx": "odg",
    "cdr": "odg",
    "cmx": "odg",
    "fh": "odg",
    "fh1": "odg",
    "fh2": "odg",
    "fh3": "odg",
    "fh4": "odg",
    "fh5": "odg",
    "fh6": "odg",
    "fh7": "odg",
    "fh8": "odg",
    "fh9": "odg",
    "fh10": "odg",
    "fh11": "odg",
    "p65": "odg",
    "pm": "odg",
    "pm6": "odg",
    "pmd": "odg",
    "qxd": "odg",
    "qxt": "odg",
    "sda": "odg",
    "std": "odg",
    "sxd": "odg",
    "wpg": "odg",
    "zmf": "odg",
}


@dataclass(frozen=True, slots=True)
class OfficeConversionResult:
    content: bytes = b""
    target_format: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.content) and not self.error


def _resolved_executable(executable: str | None) -> str | None:
    configured = str(executable or config_env("FRIDAY_LIBREOFFICE_PATH") or "").strip()
    resolved = configured or shutil.which("libreoffice") or shutil.which("soffice") or ""
    if not resolved:
        return None
    path = Path(resolved)
    if not path.is_absolute():
        return None
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def _configured_library_path() -> tuple[str | None, bool]:
    raw = str(config_env("FRIDAY_LIBREOFFICE_LIBRARY_PATH") or "").strip()
    if not raw:
        return None, True
    parts = raw.split(os.pathsep)
    if any(not part.strip() for part in parts):
        return None, False
    normalized: list[str] = []
    for part in parts:
        path = Path(part.strip())
        if not path.is_absolute():
            return None, False
        try:
            path = path.resolve(strict=True)
            mode = path.stat().st_mode
        except OSError:
            return None, False
        if not path.is_dir() or mode & stat.S_IWOTH:
            return None, False
        rendered = str(path)
        if rendered not in normalized:
            normalized.append(rendered)
    return os.pathsep.join(normalized), True


def libreoffice_available(*, executable: str | None = None) -> bool:
    """Whether the optional converter and its explicit loader path are usable."""

    _library_path, valid = _configured_library_path()
    return valid and _resolved_executable(executable) is not None


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.SubprocessError):
        process.wait(timeout=1.0)


def convert_legacy_office(
    content: bytes,
    source_format: str,
    *,
    deadline: float | None = None,
    max_output_bytes: int = _MAX_CONVERSION_OUTPUT_BYTES,
    executable: str | None = None,
) -> OfficeConversionResult:
    """Convert one allowlisted Office format into its fixed safe target."""

    normalized_source = str(source_format or "").strip().casefold().lstrip(".")
    target_format = _TARGET_BY_SOURCE.get(normalized_source, "")
    if not target_format:
        return OfficeConversionResult(error="legacy_office_conversion_unsupported")
    if not isinstance(content, bytes) or not content or len(content) > _MAX_CONVERSION_INPUT_BYTES:
        return OfficeConversionResult(target_format=target_format, error="legacy_office_input_invalid")
    resolved = _resolved_executable(executable)
    library_path, library_valid = _configured_library_path()
    if not library_valid:
        return OfficeConversionResult(
            target_format=target_format,
            error="libreoffice_configuration_invalid",
        )
    if resolved is None:
        return OfficeConversionResult(target_format=target_format, error="libreoffice_unavailable")
    common_deadline = (
        float(deadline) if deadline is not None else time.monotonic() + _DEFAULT_CONVERSION_TIMEOUT_SEC
    )
    if not math.isfinite(common_deadline) or common_deadline <= time.monotonic():
        return OfficeConversionResult(target_format=target_format, error="libreoffice_deadline_reached")
    output_limit = max(1, min(int(max_output_bytes), _MAX_CONVERSION_OUTPUT_BYTES))

    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="friday-office-",
            dir=_OFFICE_TEMP_PARENT,
        ) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            output_dir = root / "output"
            profile_dir = root / "profile"
            output_dir.mkdir(mode=0o700)
            profile_dir.mkdir(mode=0o700)
            source = root / f"source.{normalized_source}"
            source.write_bytes(content)
            source.chmod(0o600)
            environment = {
                "HOME": str(root),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TMPDIR": str(root),
            }
            if library_path is not None:
                environment["LD_LIBRARY_PATH"] = library_path
            command = [
                resolved,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--convert-to",
                target_format,
                "--outdir",
                str(output_dir),
                str(source),
            ]
            process = subprocess.Popen(  # noqa: S603 - absolute executable and fixed argv
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
            remaining = common_deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_deadline_reached",
                )
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_deadline_reached",
                )
            # A converter wrapper must not leave a child in its private process
            # group to replace or mutate the output after the parent exits.
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode != 0:
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_conversion_failed",
                )
            converted = output_dir / f"source.{target_format}"
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if not nofollow:  # pragma: no cover - deployed Linux invariant
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_output_invalid",
                )
            try:
                converted_fd = os.open(
                    converted,
                    os.O_RDONLY | os.O_CLOEXEC | nofollow,
                )
            except FileNotFoundError:
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_output_missing",
                )
            except OSError:
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_output_invalid",
                )
            try:
                converted_stat = os.fstat(converted_fd)
                if (
                    not stat.S_ISREG(converted_stat.st_mode)
                    or converted_stat.st_nlink != 1
                    or converted_stat.st_size <= 0
                ):
                    return OfficeConversionResult(
                        target_format=target_format,
                        error="libreoffice_output_invalid",
                    )
                if converted_stat.st_size > output_limit:
                    return OfficeConversionResult(
                        target_format=target_format,
                        error="libreoffice_output_too_large",
                    )
                chunks: list[bytes] = []
                total = 0
                while total <= output_limit:
                    chunk = os.read(
                        converted_fd,
                        min(64 * 1024, output_limit + 1 - total),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > output_limit:
                    return OfficeConversionResult(
                        target_format=target_format,
                        error="libreoffice_output_too_large",
                    )
                converted_content = b"".join(chunks)
                final_stat = os.fstat(converted_fd)
                if (
                    final_stat.st_dev != converted_stat.st_dev
                    or final_stat.st_ino != converted_stat.st_ino
                    or final_stat.st_size != converted_stat.st_size
                    or final_stat.st_mtime_ns != converted_stat.st_mtime_ns
                    or len(converted_content) != converted_stat.st_size
                ):
                    return OfficeConversionResult(
                        target_format=target_format,
                        error="libreoffice_output_invalid",
                    )
            except OSError:
                return OfficeConversionResult(
                    target_format=target_format,
                    error="libreoffice_output_invalid",
                )
            finally:
                with suppress(OSError):
                    os.close(converted_fd)
    except (OSError, subprocess.SubprocessError, ValueError):
        if process is not None:
            _kill_process_group(process)
        return OfficeConversionResult(
            target_format=target_format,
            error="libreoffice_conversion_failed",
        )
    return OfficeConversionResult(content=converted_content, target_format=target_format)


__all__ = [
    "OfficeConversionResult",
    "convert_legacy_office",
    "libreoffice_available",
]
