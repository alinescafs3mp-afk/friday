"""Find Friday's own credentials sitting in places they should not be.

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
  The scan is bounded by depth, file count and total bytes, and reports honestly when
  it stopped early rather than implying it looked everywhere.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Generator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

# Secrets shorter than this are not distinctive enough to match on: a four-character
# value would hit every file on the disk.
MIN_SECRET_LENGTH = 20
# Bounds. A credential left lying about is near where the owner works, not eight levels
# deep inside a dependency tree.  The former 4,000-path cap was measured against the
# actual roots and stopped 493 eligible small files too soon; 20,000 retains a hard
# bound without making traversal order decide whether the working tree is inspected.
MAX_DEPTH = 3
MAX_FILES = 20_000
# Directory entries are metadata work too.  Counting only regular candidates lets a
# single directory containing millions of symlinks/non-regular entries bypass the
# file cap before the scanner reaches one candidate.
MAX_WALK_ENTRIES = MAX_FILES * 4
# This is now the small/streamed scheduling boundary, not a skip boundary.  Small files
# are always considered before large ones; large files are read in chunks under the
# single byte budget below.
MAX_FILE_BYTES = 1_048_576
SCAN_CHUNK_BYTES = 1_048_576
MAX_SCAN_BYTES = 256 * 1_048_576

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
    # Compatibility field for callers/tests from the old skip-large scanner.  It now
    # counts large paths that could not be inspected completely (plus an oversized
    # protected value source); large candidates are streamed instead of skipped.
    oversized_skipped: int = 0
    bytes_scanned: int = 0
    files_not_fully_scanned: int = 0
    byte_budget_exhausted: bool = False
    unreadable_skipped: int = 0
    traversal_errors: int = 0
    discovery_limit_exhausted: bool = False
    # Number of logical paths covered without another physical read because they are
    # hardlinks to an inode already in the candidate set.  Findings still name every
    # path: an extra hardlink is an extra place from which a credential can escape.
    hardlink_aliases: int = 0

    @property
    def complete(self) -> bool:
        return not (
            self.stopped_early
            or self.oversized_skipped
            or self.files_not_fully_scanned
            or self.byte_budget_exhausted
            or self.unreadable_skipped
            or self.traversal_errors
            or self.discovery_limit_exhausted
        )

    @property
    def clean(self) -> bool:
        return self.complete and not self.exposures and not self.loose_permissions


def named_secrets(environ: dict[str, str] | None = None) -> dict[str, str]:
    """This instance's credentials, keyed by the variable that supplied them."""
    source = environ if environ is not None else dict(os.environ)
    secrets: dict[str, str] = {}
    for key, value in source.items():
        if not key.startswith("FRIDAY_"):
            continue
        if not any(marker in key for marker in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")):
            continue
        cleaned = value.strip()
        if len(cleaned) >= MIN_SECRET_LENGTH:
            secrets[key] = cleaned
    return secrets


def _candidate_files(roots: Iterable[Path], *, report: Report) -> Generator[Path, None, None]:
    """Yield each file once, even when one root sits inside another.

    ``FRIDAY_HOME`` normally lives under the owner's home directory, so scanning both
    walks the same tree twice and reports every finding twice.
    """
    seen_roots: set[Path] = set()
    walked_directories: set[Path] = set()
    emitted: set[Path] = set()
    entries_seen = 0
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            report.traversal_errors += 1
            continue
        if resolved in seen_roots:
            continue
        try:
            if not stat_module.S_ISDIR(resolved.stat().st_mode):
                report.traversal_errors += 1
                continue
        except OSError:
            report.traversal_errors += 1
            continue
        seen_roots.add(resolved)
        pending: list[tuple[Path, int]] = [(resolved, 0)]
        while pending:
            here, depth = pending.pop()
            if here in walked_directories:
                continue
            walked_directories.add(here)
            descriptor: int | None = None
            try:
                scan_target: int | Path = here
                if os.scandir in os.supports_fd:
                    flags = (
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    descriptor = os.open(here, flags)
                    scan_target = descriptor
                with os.scandir(scan_target) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > MAX_WALK_ENTRIES:
                            report.discovery_limit_exhausted = True
                            return
                        candidate = here / entry.name
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            report.traversal_errors += 1
                            continue
                        if is_directory:
                            if depth < MAX_DEPTH and entry.name not in SKIP_DIRECTORIES:
                                pending.append((candidate, depth + 1))
                            continue
                        if candidate in emitted:
                            continue
                        emitted.add(candidate)
                        yield candidate
            except OSError:
                report.traversal_errors += 1
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)


@dataclass(frozen=True)
class _Candidate:
    path: Path
    mode: int
    size: int
    device: int
    inode: int


@dataclass(frozen=True)
class _ScanResult:
    secret_name: str | None
    bytes_read: int
    complete: bool
    unreadable: bool = False


@dataclass(frozen=True)
class _ProtectedRead:
    value: str | None
    mode: int
    bytes_read: int
    complete: bool
    oversized: bool = False
    unreadable: bool = False
    excluded: bool = False


def _regular_inodes(paths: Iterable[Path]) -> set[tuple[int, int]]:
    """Take a metadata-only snapshot of regular files at exact excluded paths."""

    identities: set[tuple[int, int]] = set()
    for path in paths:
        try:
            file_stat = path.stat()
        except OSError:
            continue
        if stat_module.S_ISREG(file_stat.st_mode):
            identities.add((int(file_stat.st_dev), int(file_stat.st_ino)))
    return identities


def _collect_candidates(
    roots: Iterable[Path],
    *,
    protected_paths: set[Path],
    excluded_paths: set[Path],
    excluded_inodes: set[tuple[int, int]],
    report: Report,
) -> list[_Candidate]:
    """Collect a bounded regular-file snapshot without following symlinks."""

    candidates: list[_Candidate] = []
    paths = _candidate_files(roots, report=report)
    try:
        for path in paths:
            try:
                file_stat = path.lstat()
                if not stat_module.S_ISREG(file_stat.st_mode):
                    continue
                resolved = path.resolve()
                inode = (int(file_stat.st_dev), int(file_stat.st_ino))
                if resolved in excluded_paths:
                    # An excluded WAL/SHM can appear after the initial exclusion
                    # snapshot.  Remember its now-known identity and filter aliases
                    # collected earlier once discovery is complete.
                    excluded_inodes.add(inode)
                    continue
                if inode in excluded_inodes:
                    continue
                if resolved in protected_paths:
                    continue
            except OSError:
                report.unreadable_skipped += 1
                report.files_not_fully_scanned += 1
                continue
            if len(candidates) >= MAX_FILES:
                report.stopped_early = True
                break
            candidates.append(
                _Candidate(
                    path=path,
                    mode=file_stat.st_mode & 0o777,
                    size=max(0, int(file_stat.st_size)),
                    device=int(file_stat.st_dev),
                    inode=int(file_stat.st_ino),
                )
            )
    finally:
        paths.close()
    return candidates


def _scan_regular_file(
    candidate: _Candidate,
    needles: list[tuple[str, bytes]],
    *,
    byte_limit: int,
) -> _ScanResult:
    """Scan one inode in chunks; return match name, physical bytes read, completeness.

    The overlap is derived from the longest exact credential, so a value split at any
    chunk boundary is still visible.  Opening and then checking ``fstat`` closes the
    ordinary path-to-symlink replacement race: a changed candidate is not read under
    the authority of an earlier ``lstat``.
    """

    best_index: int | None = None
    longest = max((len(needle) for _name, needle in needles), default=1)
    overlap = max(0, longest - 1)
    consumed = 0
    tail = b""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate.path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened_stat.st_mode)
            or int(opened_stat.st_dev) != candidate.device
            or int(opened_stat.st_ino) != candidate.inode
        ):
            raise OSError("candidate changed while opening")
        while consumed < byte_limit:
            try:
                chunk = os.read(descriptor, min(SCAN_CHUNK_BYTES, byte_limit - consumed))
            except OSError:
                # Once any alias has been opened, never retry the same inode through
                # another hardlink: bytes already read must remain charged to the
                # global budget even when a later read fails.
                name = needles[best_index][0] if best_index is not None else None
                return _ScanResult(name, consumed, False, unreadable=True)
            if not chunk:
                name = needles[best_index][0] if best_index is not None else None
                return _ScanResult(name, consumed, True)
            consumed += len(chunk)
            window = tail + chunk
            for index, (_name, needle) in enumerate(needles):
                if (best_index is None or index < best_index) and needle in window:
                    best_index = index
            # The first needle is globally the longest/most specific.  Once it is
            # found, reading the rest cannot change the one-finding-per-path result.
            if best_index == 0:
                return _ScanResult(needles[0][0], consumed, True)
            tail = window[-overlap:] if overlap else b""

        try:
            opened_size = max(0, int(os.fstat(descriptor).st_size))
        except OSError:
            name = needles[best_index][0] if best_index is not None else None
            return _ScanResult(name, consumed, False, unreadable=True)
        name = needles[best_index][0] if best_index is not None else None
        return _ScanResult(name, consumed, consumed >= opened_size)
    finally:
        # A late close error must not erase already-accounted bytes and trigger a
        # second physical read through another hardlink.
        with suppress(OSError):
            os.close(descriptor)


def _read_protected_value(
    candidate: _Candidate,
    *,
    byte_limit: int,
    excluded_inodes: set[tuple[int, int]],
) -> _ProtectedRead:
    """Read one configured secret source without following a replacement symlink.

    Protected files supply additional exact needles, so their reads are part of the
    same global byte budget.  ``excluded`` remains stronger than ``protected`` even
    when the two names are hardlinks to one inode.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate.path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        identity = (int(opened_stat.st_dev), int(opened_stat.st_ino))
        if not stat_module.S_ISREG(opened_stat.st_mode) or identity != (candidate.device, candidate.inode):
            raise OSError("protected file changed while opening")
        mode = opened_stat.st_mode & 0o777
        if identity in excluded_inodes:
            return _ProtectedRead(None, mode, 0, True, excluded=True)
        if max(0, int(opened_stat.st_size)) > MAX_FILE_BYTES:
            return _ProtectedRead(None, mode, 0, False, oversized=True)
        if byte_limit <= 0:
            return _ProtectedRead(None, mode, 0, False)

        payload = bytearray()
        read_limit = min(MAX_FILE_BYTES + 1, byte_limit)
        reached_eof = False
        unreadable = False
        while len(payload) < read_limit:
            try:
                chunk = os.read(descriptor, min(SCAN_CHUNK_BYTES, read_limit - len(payload)))
            except OSError:
                unreadable = True
                break
            if not chunk:
                reached_eof = True
                break
            payload.extend(chunk)

        oversized = len(payload) > MAX_FILE_BYTES
        complete = reached_eof
        if not complete and not unreadable and not oversized:
            try:
                complete = len(payload) >= max(0, int(os.fstat(descriptor).st_size))
            except OSError:
                unreadable = True
        value = None
        if complete and not oversized and not unreadable:
            # Preserve undecodable filesystem bytes losslessly.  Dropping them could
            # turn one configured credential into a shorter, unrelated needle.
            value = bytes(payload).decode("utf-8", "surrogateescape").strip()
        return _ProtectedRead(
            value,
            mode,
            len(payload),
            complete,
            oversized=oversized,
            unreadable=unreadable,
        )
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _redact_report_values(report: Report, values: Iterable[str]) -> Report:
    """Ensure attacker-controlled labels and path components cannot echo a secret."""

    needles = sorted({value for value in values if value}, key=len, reverse=True)
    if not needles:
        return report

    def cleaned(raw: str) -> str:
        for needle in needles:
            raw = raw.replace(needle, "<redacted-secret>")
        return raw

    report.exposures = [
        Exposure(
            path=Path(cleaned(str(item.path))),
            secret_name=cleaned(item.secret_name),
            mode=item.mode,
        )
        for item in report.exposures
    ]
    report.loose_permissions = [(Path(cleaned(str(path))), mode) for path, mode in report.loose_permissions]
    return report


def scan(
    roots: Iterable[Path],
    *,
    secrets: dict[str, str] | None = None,
    protected: Iterable[Path] = (),
    excluded: Iterable[Path] = (),
) -> Report:
    """Look for this instance's secrets in files, and for secret files left readable.

    ``protected`` names the files that are SUPPOSED to hold credentials — the env file,
    the backup key. Those are not exposures; their permissions are what matter.

    ``excluded`` is a stronger process-safety boundary for live databases and similar
    artifacts: neither the named path nor another observed hardlink to the same inode is
    opened at all.  Unlike ``protected``, an excluded file is not read as a source of
    secret values and its permissions are not interpreted here.
    """
    values = dict(secrets if secrets is not None else named_secrets())
    report = Report(exposures=[], loose_permissions=[], files_scanned=0, stopped_early=False)
    protected_paths: set[Path] = set()
    for path in protected:
        if not path:
            continue
        try:
            protected_paths.add(path.expanduser().resolve())
        except OSError:
            report.unreadable_skipped += 1
            report.files_not_fully_scanned += 1
    excluded_paths: set[Path] = set()
    excluded_inodes: set[tuple[int, int]] = set()
    for path in excluded:
        if not path:
            continue
        try:
            resolved = path.expanduser().resolve()
            excluded_paths.add(resolved)
            file_stat = resolved.stat()
        except OSError:
            # Keep a lexical absolute form for a WAL/SHM file that does not exist yet;
            # it can appear between this snapshot and candidate traversal.
            with suppress(OSError):
                excluded_paths.add(path.expanduser().absolute())
            continue
        if stat_module.S_ISREG(file_stat.st_mode):
            excluded_inodes.add((int(file_stat.st_dev), int(file_stat.st_ino)))

    # A protected file's CONTENTS are a secret too, and the most sensitive one here:
    # `named_secrets` only collects environment variables whose NAME carries
    # TOKEN/SECRET/API_KEY/PASSWORD, and the backup encryption key is configured as
    # FRIDAY_BACKUP_ENCRYPTION_KEY_FILE — a path. So the scanner knew where the key
    # was and checked its permissions, while a 0644 copy of the key itself beside it
    # was invisible. `jericho keygen` advises making that copy.
    # Read those sources through the same bounded descriptor discipline as candidates.
    # `excluded` is checked before open, and once more against the descriptor identity.
    remaining = MAX_SCAN_BYTES
    excluded_inodes.update(_regular_inodes(excluded_paths))
    for path in sorted(protected_paths, key=str):
        if path in excluded_paths:
            continue
        try:
            file_stat = path.lstat()
            identity = (int(file_stat.st_dev), int(file_stat.st_ino))
            if not stat_module.S_ISREG(file_stat.st_mode):
                raise OSError("protected path is not a regular file")
            if identity in excluded_inodes:
                continue
            protected_result = _read_protected_value(
                _Candidate(
                    path=path,
                    mode=file_stat.st_mode & 0o777,
                    size=max(0, int(file_stat.st_size)),
                    device=identity[0],
                    inode=identity[1],
                ),
                byte_limit=remaining,
                excluded_inodes=excluded_inodes,
            )
        except OSError:
            report.unreadable_skipped += 1
            report.files_not_fully_scanned += 1
            continue
        if protected_result.excluded:
            continue
        if protected_result.mode & 0o077:
            report.loose_permissions.append((path, protected_result.mode))
        report.bytes_scanned += protected_result.bytes_read
        remaining -= protected_result.bytes_read
        if protected_result.oversized:
            report.oversized_skipped += 1
            report.files_not_fully_scanned += 1
            continue
        if protected_result.unreadable or not protected_result.complete:
            report.files_not_fully_scanned += 1
            if protected_result.unreadable:
                report.unreadable_skipped += 1
            if remaining <= 0:
                report.byte_budget_exhausted = True
            continue
        text = protected_result.value or ""
        if len(text) >= MIN_SECRET_LENGTH and text not in values.values():
            values[f"содержимое {path.name}"] = text

    if not values:
        return _redact_report_values(report, values.values())

    # Longest first: a file containing several secrets is reported for the most
    # specific one rather than for whichever variable happened to be iterated first.
    ordered = sorted(values.items(), key=lambda item: len(item[1]), reverse=True)
    encoded = [(name, value.encode("utf-8", "surrogateescape")) for name, value in ordered]

    candidates = _collect_candidates(
        roots,
        protected_paths=protected_paths,
        excluded_paths=excluded_paths,
        excluded_inodes=excluded_inodes,
        report=report,
    )
    # Exact excluded sidecars can be created while the filesystem walk is in
    # progress.  Refresh their current inode identities before opening any collected
    # alias, and re-filter aliases found before the exact path appeared.
    excluded_inodes.update(_regular_inodes(excluded_paths))
    candidates = [
        candidate for candidate in candidates if (candidate.device, candidate.inode) not in excluded_inodes
    ]
    by_inode: dict[tuple[int, int], list[_Candidate]] = {}
    for candidate in candidates:
        by_inode.setdefault((candidate.device, candidate.inode), []).append(candidate)
    groups = list(by_inode.values())
    report.hardlink_aliases = sum(max(0, len(group) - 1) for group in groups)

    # The old traversal spent its whole file-count budget in whichever directory
    # `os.walk` happened to visit first and never opened a later small config.  Once
    # candidates are bounded, schedule every small inode before any large one, then
    # stream large files from smallest to largest so a giant database cannot starve
    # ordinary exports and logs.
    groups.sort(
        key=lambda group: (
            group[0].size > MAX_FILE_BYTES,
            group[0].size,
            str(group[0].path),
        )
    )
    for index, group in enumerate(groups):
        aliases = len(group)
        if remaining <= 0:
            report.byte_budget_exhausted = True
            unvisited = groups[index:]
            report.files_not_fully_scanned += sum(len(items) for items in unvisited)
            report.oversized_skipped += sum(
                len(items) for items in unvisited if items[0].size > MAX_FILE_BYTES
            )
            break

        scanned: _ScanResult | None = None
        # Parent-directory permissions can make one hardlink path unreadable while a
        # sibling alias remains usable.  Try each alias, but read the inode at most once.
        for candidate in group:
            try:
                scanned = _scan_regular_file(candidate, encoded, byte_limit=remaining)
                break
            except OSError:
                continue
        if scanned is None:
            report.unreadable_skipped += aliases
            report.files_not_fully_scanned += aliases
            if group[0].size > MAX_FILE_BYTES:
                report.oversized_skipped += aliases
            continue

        report.bytes_scanned += scanned.bytes_read
        remaining -= scanned.bytes_read
        if scanned.secret_name is not None:
            report.exposures.extend(
                Exposure(path=item.path, secret_name=scanned.secret_name, mode=item.mode) for item in group
            )
        if scanned.complete:
            report.files_scanned += aliases
            continue

        report.files_not_fully_scanned += aliases
        if scanned.unreadable:
            report.unreadable_skipped += aliases
        if group[0].size > MAX_FILE_BYTES:
            report.oversized_skipped += aliases
        if remaining <= 0:
            report.byte_budget_exhausted = True
    return _redact_report_values(report, values.values())
