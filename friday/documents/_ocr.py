"""Bounded local OCR over already-normalized visual assets.

The optional Tesseract executable is an independent fallback for scans when the
vision model is unavailable or returns no usable page carrier.  Input reaches
the fixed executable through stdin, output is read through a byte-capped pipe,
and one monotonic deadline owns every page in the supplied prefix.
"""

from __future__ import annotations

import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from friday.config import env as config_env

_MAX_OCR_OUTPUT_BYTES = 1_000_000
_MAX_OCR_PAGES = 40
_OCR_PAGE_TIMEOUT_SEC = 30.0
_OCR_LANGUAGE_PROBE_TIMEOUT_SEC = 5.0
_MAX_LANGUAGE_PROBE_OUTPUT_BYTES = 64 * 1024
_OCR_LANGUAGE_RE = re.compile(r"[a-z0-9_]+(?:\+[a-z0-9_]+)*", re.ASCII)
_OCR_SINGLE_LANGUAGE_RE = re.compile(r"[a-z0-9_]+", re.ASCII)
_LANGUAGE_CACHE_LIMIT = 32
_LANGUAGE_CACHE: dict[tuple[str, str, str, str], frozenset[str]] = {}
_LANGUAGE_CACHE_LOCK = threading.Lock()


class _VisualAsset(Protocol):
    @property
    def data(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LocalOcrResult:
    """A contiguous prefix of locally transcribed visual assets."""

    page_texts: tuple[str, ...]
    pages_total: int
    pages_read: int
    text_truncated: bool = False
    deadline_reached: bool = False
    error: str = ""
    page_cap_reached: bool = False

    @property
    def success(self) -> bool:
        return bool(self.page_texts) and (
            self.pages_read == self.pages_total == len(self.page_texts)
            and not self.text_truncated
            and not self.deadline_reached
            and not self.page_cap_reached
            and not self.error
        )


def _resolved_executable(executable: str | None) -> str | None:
    configured = str(executable or config_env("FRIDAY_TESSERACT_PATH") or "").strip()
    resolved = configured or shutil.which("tesseract") or ""
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


def _absolute_directory(value: str) -> str | None:
    """Resolve one existing absolute directory without preserving a symlink."""

    path = Path(value)
    if not value or not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return str(resolved) if resolved.is_dir() else None


def _configured_tessdata() -> tuple[str | None, str | None, bool]:
    """Return explicit CLI/standard tessdata locations and config validity."""

    custom = str(config_env("FRIDAY_TESSDATA_DIR") or "").strip()
    custom_path = _absolute_directory(custom)
    if custom:
        # The Friday-specific CLI value is authoritative.  Do not let an
        # unrelated ambient TESSDATA_PREFIX invalidate or influence it.
        return custom_path, None, custom_path is not None
    standard = str(os.environ.get("TESSDATA_PREFIX") or "").strip()
    standard_path = _absolute_directory(standard)
    return None, standard_path, not standard or standard_path is not None


def _configured_library_path() -> tuple[str | None, bool]:
    """Build an opt-in loader path without inheriting ambient linker state.

    Rootless package extraction needs its private ``lib`` directories before
    the Tesseract process starts.  Every element must already exist, be
    absolute, and not be world-writable.  The normalized paths are the only
    value forwarded to ``LD_LIBRARY_PATH``; the parent's variable is ignored.
    """

    raw = str(config_env("FRIDAY_TESSERACT_LIBRARY_PATH") or "").strip()
    if not raw:
        return None, True
    parts = raw.split(os.pathsep)
    if any(not part.strip() for part in parts):
        return None, False
    resolved_parts: list[str] = []
    for part in parts:
        resolved = _absolute_directory(part.strip())
        if resolved is None:
            return None, False
        try:
            mode = Path(resolved).stat().st_mode
        except OSError:
            return None, False
        if mode & stat.S_IWOTH:
            return None, False
        if resolved not in resolved_parts:
            resolved_parts.append(resolved)
    return os.pathsep.join(resolved_parts), True


def _configured_language() -> str | None:
    language = str(config_env("FRIDAY_TESSERACT_LANGUAGES") or "rus+eng").strip().casefold()
    return language if _OCR_LANGUAGE_RE.fullmatch(language) is not None else None


def _traineddata_available(language: str, tessdata_dir: str | None) -> bool:
    """Validate explicitly selected language packs before starting a process."""

    if tessdata_dir is None:
        # A system Tesseract can use its compiled-in data directory.  There is
        # no reliable filesystem path to inspect until the binary runs.
        return True
    root = Path(tessdata_dir)
    for name in language.split("+"):
        candidate = root / f"{name}.traineddata"
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def local_ocr_available(*, executable: str | None = None) -> bool:
    """Whether executable, loader directories and explicit language data are usable."""

    resolved = _resolved_executable(executable)
    if resolved is None:
        return False
    language = _configured_language()
    tessdata_dir, standard_tessdata, tessdata_valid = _configured_tessdata()
    library_path, library_path_valid = _configured_library_path()
    selected_tessdata = tessdata_dir or standard_tessdata
    if not (
        language
        and tessdata_valid
        and library_path_valid
        and _traineddata_available(language, selected_tessdata)
    ):
        return False
    languages, _error = _listed_languages(
        resolved,
        tessdata_dir=tessdata_dir,
        standard_tessdata=standard_tessdata,
        library_path=library_path,
        deadline=time.monotonic() + _OCR_LANGUAGE_PROBE_TIMEOUT_SEC,
    )
    return set(language.split("+")).issubset(languages)


def _bounded_process(
    command: list[str],
    payload: bytes,
    *,
    deadline: float,
    output_limit: int,
    environment: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Feed stdin and drain stdout concurrently under exact byte/time ceilings."""

    process = subprocess.Popen(  # noqa: S603 - absolute executable plus fixed argv
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **(environment or {})},
    )
    try:
        if process.stdin is None or process.stdout is None:  # pragma: no cover - Popen invariant
            raise OSError("local OCR pipes unavailable")
        input_fd = process.stdin.fileno()
        output_fd = process.stdout.fileno()
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)
        input_offset = 0
        output = bytearray()
        input_open = True
        output_open = True
        with selectors.DefaultSelector() as selector:
            selector.register(input_fd, selectors.EVENT_WRITE, "input")
            selector.register(output_fd, selectors.EVENT_READ, "output")
            while output_open:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("local OCR deadline reached")
                events = selector.select(min(0.1, remaining))
                if not events and process.poll() is not None:
                    # The child has exited; one non-blocking read below will
                    # observe any buffered tail or EOF on the next iteration.
                    events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
                for key, _mask in events:
                    if key.data == "input" and input_open:
                        try:
                            written = os.write(input_fd, payload[input_offset : input_offset + 64 * 1024])
                        except (BlockingIOError, BrokenPipeError):
                            written = 0
                            if isinstance(process.poll(), int):
                                input_offset = len(payload)
                        input_offset += written
                        if input_offset >= len(payload):
                            selector.unregister(input_fd)
                            process.stdin.close()
                            input_open = False
                    elif key.data == "output" and output_open:
                        try:
                            chunk = os.read(output_fd, min(64 * 1024, output_limit + 1 - len(output)))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(output_fd)
                            output_open = False
                            break
                        output.extend(chunk)
                        if len(output) > output_limit:
                            raise ValueError("local OCR output limit exceeded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("local OCR deadline reached")
        return_code = process.wait(timeout=remaining)
        return return_code, bytes(output)
    except BaseException:
        if process.poll() is None:
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
        with suppress(OSError, subprocess.SubprocessError):
            process.wait(timeout=1.0)
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()


def _listed_languages(
    executable: str,
    *,
    tessdata_dir: str | None,
    standard_tessdata: str | None,
    library_path: str | None,
    deadline: float,
) -> tuple[frozenset[str], str]:
    """Ask the exact executable to prove its effective traineddata set."""

    cache_key = (
        executable,
        tessdata_dir or "",
        standard_tessdata or "",
        library_path or "",
    )
    with _LANGUAGE_CACHE_LOCK:
        cached = _LANGUAGE_CACHE.get(cache_key)
    if cached is not None:
        return cached, ""
    tessdata_args = ["--tessdata-dir", tessdata_dir] if tessdata_dir is not None else []
    environment = {"TESSDATA_PREFIX": standard_tessdata} if standard_tessdata is not None else {}
    if library_path is not None:
        environment["LD_LIBRARY_PATH"] = library_path
    try:
        return_code, output = _bounded_process(
            [executable, "--list-langs", *tessdata_args],
            b"",
            deadline=deadline,
            output_limit=_MAX_LANGUAGE_PROBE_OUTPUT_BYTES,
            environment=environment,
        )
    except TimeoutError:
        return frozenset(), "local_ocr_deadline_reached"
    except (OSError, subprocess.SubprocessError, ValueError):
        return frozenset(), "local_ocr_configuration_invalid"
    if return_code != 0:
        return frozenset(), "local_ocr_configuration_invalid"
    decoded = output.decode("utf-8", errors="replace")
    languages = frozenset(
        line.strip().casefold()
        for line in decoded.splitlines()
        if _OCR_SINGLE_LANGUAGE_RE.fullmatch(line.strip().casefold()) is not None
    )
    if not languages:
        return frozenset(), "local_ocr_configuration_invalid"
    with _LANGUAGE_CACHE_LOCK:
        if len(_LANGUAGE_CACHE) >= _LANGUAGE_CACHE_LIMIT:
            _LANGUAGE_CACHE.pop(next(iter(_LANGUAGE_CACHE)))
        _LANGUAGE_CACHE[cache_key] = languages
    return languages, ""


def _clean_ocr_text(payload: bytes) -> str:
    decoded = payload.decode("utf-8", errors="replace").replace("\f", "\n")
    safe = "".join(character for character in decoded if character in "\n\t" or character.isprintable())
    lines = [line.rstrip() for line in safe.splitlines()]
    return "\n".join(lines).strip()


def extract_local_ocr(
    assets: Sequence[_VisualAsset],
    *,
    max_text_chars: int,
    deadline: float | None = None,
    executable: str | None = None,
) -> LocalOcrResult:
    """Transcribe an ordered asset prefix with optional local Tesseract.

    The Russian+English language set is intentional for Friday's deployed
    corpus.  A missing language pack fails closed instead of silently returning
    English-shaped mojibake for a Russian document.
    """

    pages_total = len(assets)
    bounded_assets = tuple(assets[:_MAX_OCR_PAGES])
    page_cap_reached = pages_total > len(bounded_assets)
    resolved = _resolved_executable(executable)
    if resolved is None:
        return LocalOcrResult(
            (),
            pages_total,
            0,
            error="local_ocr_unavailable",
            page_cap_reached=page_cap_reached,
        )
    language = _configured_language()
    if language is None:
        return LocalOcrResult(
            (),
            pages_total,
            0,
            error="local_ocr_configuration_invalid",
            page_cap_reached=page_cap_reached,
        )
    tessdata_dir, standard_tessdata, tessdata_valid = _configured_tessdata()
    library_path, library_path_valid = _configured_library_path()
    if (
        not tessdata_valid
        or not library_path_valid
        or not _traineddata_available(language, tessdata_dir or standard_tessdata)
    ):
        return LocalOcrResult(
            (),
            pages_total,
            0,
            error="local_ocr_configuration_invalid",
            page_cap_reached=page_cap_reached,
        )
    tessdata_args = ["--tessdata-dir", tessdata_dir] if tessdata_dir is not None else []
    child_environment = {"TESSDATA_PREFIX": standard_tessdata} if standard_tessdata is not None else {}
    if library_path is not None:
        child_environment["LD_LIBRARY_PATH"] = library_path
    common_deadline = (
        float(deadline)
        if deadline is not None
        else time.monotonic() + _OCR_PAGE_TIMEOUT_SEC
    )
    if not math.isfinite(common_deadline) or common_deadline <= time.monotonic():
        return LocalOcrResult(
            (),
            pages_total,
            0,
            deadline_reached=True,
            error="local_ocr_deadline_reached",
            page_cap_reached=page_cap_reached,
        )
    available_languages, language_error = _listed_languages(
        resolved,
        tessdata_dir=tessdata_dir,
        standard_tessdata=standard_tessdata,
        library_path=library_path,
        deadline=min(
            common_deadline,
            time.monotonic() + _OCR_LANGUAGE_PROBE_TIMEOUT_SEC,
        ),
    )
    if not set(language.split("+")).issubset(available_languages):
        deadline_reached = language_error == "local_ocr_deadline_reached"
        return LocalOcrResult(
            (),
            pages_total,
            0,
            deadline_reached=deadline_reached,
            error=language_error or "local_ocr_configuration_invalid",
            page_cap_reached=page_cap_reached,
        )
    text_limit = max(1_000, min(int(max_text_chars), 2_000_000))
    texts: list[str] = []
    text_chars = 0
    truncated = False
    for asset in bounded_assets:
        remaining = common_deadline - time.monotonic()
        if remaining <= 0:
            return LocalOcrResult(
                tuple(texts),
                pages_total,
                len(texts),
                text_truncated=truncated,
                deadline_reached=True,
                error="local_ocr_deadline_reached",
                page_cap_reached=page_cap_reached,
            )
        page_deadline = time.monotonic() + min(remaining, _OCR_PAGE_TIMEOUT_SEC)
        try:
            return_code, raw_text = _bounded_process(
                [
                    resolved,
                    "stdin",
                    "stdout",
                    *tessdata_args,
                    "-l",
                    language,
                    "--psm",
                    "3",
                ],
                bytes(asset.data),
                deadline=page_deadline,
                output_limit=min(_MAX_OCR_OUTPUT_BYTES, max(1_000, text_limit * 4)),
                environment=child_environment,
            )
        except TimeoutError:
            return LocalOcrResult(
                tuple(texts),
                pages_total,
                len(texts),
                text_truncated=truncated,
                deadline_reached=True,
                error="local_ocr_deadline_reached",
                page_cap_reached=page_cap_reached,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return LocalOcrResult(
                tuple(texts),
                pages_total,
                len(texts),
                text_truncated=truncated,
                error="local_ocr_failed",
                page_cap_reached=page_cap_reached,
            )
        if return_code != 0:
            return LocalOcrResult(
                tuple(texts),
                pages_total,
                len(texts),
                text_truncated=truncated,
                error="local_ocr_failed",
                page_cap_reached=page_cap_reached,
            )
        page_text = _clean_ocr_text(raw_text)
        if not page_text:
            return LocalOcrResult(
                tuple(texts),
                pages_total,
                len(texts),
                text_truncated=truncated,
                error="local_ocr_page_text_empty",
                page_cap_reached=page_cap_reached,
            )
        separator_chars = 2 if texts else 0
        available = text_limit - text_chars - separator_chars
        if available <= 0:
            truncated = True
            break
        clipped = page_text[:available]
        texts.append(clipped)
        text_chars += separator_chars + len(clipped)
        if len(clipped) != len(page_text):
            truncated = True
            break
    return LocalOcrResult(
        tuple(texts),
        pages_total,
        len(texts),
        text_truncated=truncated,
        error=(
            "local_ocr_page_cap_reached"
            if page_cap_reached
            else "local_ocr_text_truncated"
            if truncated
            else ""
            if texts
            else "local_ocr_page_text_empty"
        ),
        page_cap_reached=page_cap_reached,
    )


__all__ = ["LocalOcrResult", "extract_local_ocr", "local_ocr_available"]
