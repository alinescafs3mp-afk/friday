"""Find Jericho's own credentials sitting in places they should not be.

A live Telegram bot token spent two days in a plain file on the desktop of this very
machine, inside a directory its owner then asked to have imported into the knowledge
base. Nothing noticed. ``jericho doctor`` inspected the database, the workers, the
backups and the model endpoint, and had no opinion at all about whether the secrets it
runs on were lying around in the open.

So this looks for them by value. It knows exactly what the credentials are — they are
in the environment it was started with — and a byte-for-byte match is not a heuristic
that can cry wolf: a file either contains this instance's bot token or it does not.

Two rules shape the implementation:

* **Never widen the exposure.** A finding reports a path and a name. The value is never
  logged, never returned, never compared in a way that could print it.
* **Never become the slow part.** ``doctor`` is run when something is already wrong.
  The scan is bounded by depth, by file count and by file size, and reports honestly
  when it stopped early rather than implying it looked everywhere.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Secrets shorter than this are not distinctive enough to match on: a four-character
# value would hit every file on the disk.
MIN_SECRET_LENGTH = 20
# Bounds. A credential left lying about is near where the owner works, not eight levels
# deep inside a dependency tree.
MAX_DEPTH = 3
MAX_FILES = 4000
MAX_FILE_BYTES = 1_048_576

# Directories that are never where a person leaves a note to themselves, and always
# where a scan goes to die.
SKIP_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".local",
        ".mozilla",
        ".npm",
        ".steam",
        ".thunderbird",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "snap",
        "venv",
    }
)


# The file the process was configured from is where these values belong — and it is
# resolved, not guessed by name. Skipping every file NAMED `.env` or `.env.local`
# anywhere in the tree was blindness precisely where credentials live: a copy of a live
# token in some unrelated project's `.env` went unreported while the same token in
# `env.txt` beside it was reported. Backups of the real env file (`.env.local.bak.*`)
# are deliberately NOT covered either — an extra copy of a live credential is exactly
# the thing worth reporting.


@dataclass(frozen=True)
class Exposure:
    """A file that contains one of this instance's secrets."""

    path: Path
    secret_name: str
    mode: int

    @property
    def world_readable(self) -> bool:
        return bool(self.mode & 0o044)


@dataclass
class Report:
    exposures: list[Exposure]
    loose_permissions: list[tuple[Path, int]]
    files_scanned: int
    stopped_early: bool
    # Files over `MAX_FILE_BYTES` that the scan never opened at all — silently,
    # before this counter existed. A large file is not an unusual place for a
    # secret to end up (a config dump, a log, an exported browser profile), and
    # `exposures`/`clean` said nothing about the ones this number counts.
    oversized_skipped: int = 0

    @property
    def clean(self) -> bool:
        return not self.exposures and not self.loose_permissions


def named_secrets(environ: dict[str, str] | None = None) -> dict[str, str]:
    """This instance's credentials, keyed by the variable that supplied them."""
    source = environ if environ is not None else dict(os.environ)
    secrets: dict[str, str] = {}
    for key, value in source.items():
        if not key.startswith("JERICHO_"):
            continue
        if not any(marker in key for marker in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")):
            continue
        cleaned = value.strip()
        if len(cleaned) >= MIN_SECRET_LENGTH:
            secrets[key] = cleaned
    return secrets


def _candidate_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield each file once, even when one root sits inside another.

    ``JERICHO_HOME`` normally lives under the owner's home directory, so scanning both
    walks the same tree twice and reports every finding twice.
    """
    seen_roots: set[Path] = set()
    emitted: set[Path] = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        base_depth = len(resolved.parts)
        for current, directories, files in os.walk(resolved, topdown=True, followlinks=False):
            here = Path(current)
            if len(here.parts) - base_depth >= MAX_DEPTH:
                directories[:] = []
            directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
            for name in files:
                candidate = here / name
                if candidate in emitted:
                    continue
                emitted.add(candidate)
                yield candidate


def scan(
    roots: Iterable[Path],
    *,
    secrets: dict[str, str] | None = None,
    protected: Iterable[Path] = (),
) -> Report:
    """Look for this instance's secrets in files, and for secret files left readable.

    ``protected`` names the files that are SUPPOSED to hold credentials — the env file,
    the backup key. Those are not exposures; their permissions are what matter.
    """
    values = secrets if secrets is not None else named_secrets()
    protected_paths = {path.expanduser().resolve() for path in protected if path}
    report = Report(
        exposures=[], loose_permissions=[], files_scanned=0, stopped_early=False, oversized_skipped=0
    )

    for path in protected_paths:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            report.loose_permissions.append((path, mode))

    # A protected file's CONTENTS are a secret too, and the most sensitive one here:
    # `named_secrets` only collects environment variables whose NAME carries
    # TOKEN/SECRET/API_KEY/PASSWORD, and the backup encryption key is configured as
    # JERICHO_BACKUP_ENCRYPTION_KEY_FILE — a path. So the scanner knew where the key
    # was and checked its permissions, while a 0644 copy of the key itself beside it
    # was invisible. `jericho keygen` advises making that copy.
    values = dict(values)
    for path in protected_paths:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                report.oversized_skipped += 1
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if len(text) >= MIN_SECRET_LENGTH and text not in values.values():
            values[f"содержимое {path.name}"] = text

    if not values:
        return report

    # Longest first: a file containing several secrets is reported for the most
    # specific one rather than for whichever variable happened to be iterated first.
    ordered = sorted(values.items(), key=lambda item: len(item[1]), reverse=True)
    encoded = [(name, value.encode("utf-8", "ignore")) for name, value in ordered]

    for path in _candidate_files(roots):
        if report.files_scanned >= MAX_FILES:
            report.stopped_early = True
            break
        try:
            stat = path.stat()
            if not path.is_file() or path.is_symlink():
                continue
            if stat.st_size > MAX_FILE_BYTES:
                report.oversized_skipped += 1
                continue
            if path.resolve() in protected_paths:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        report.files_scanned += 1
        for name, needle in encoded:
            if needle in blob:
                report.exposures.append(Exposure(path=path, secret_name=name, mode=stat.st_mode & 0o777))
                break
    return report
