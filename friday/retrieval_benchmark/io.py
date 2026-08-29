"""Bounded transient JSON/JSONL input for the offline benchmark."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable, Iterable
from itertools import islice
from pathlib import Path
from typing import Final, TypeVar

from friday.retrieval_benchmark._canonical import (
    MAX_JSONL_BYTES,
    MAX_JSONL_ITEMS,
    RecallContractError,
)
from friday.retrieval_benchmark.contracts import (
    RecallCaseV1,
    RecallObservationV1,
    RecallReportV1,
)

MAX_JSONL_LINE_BYTES: Final = 262_144
MAX_REPORT_BYTES: Final = 524_288
MAX_OUTPUT_ITEMS: Final = 2
MAX_OUTPUT_ITEM_BYTES: Final = MAX_JSONL_BYTES
MAX_OUTPUT_TOTAL_BYTES: Final = MAX_OUTPUT_ITEMS * MAX_OUTPUT_ITEM_BYTES
ContractT = TypeVar("ContractT")
_PRIVATE_FILE_MODE: Final = 0o600
_STABLE_FIELDS: Final = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _stable_identity(status: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(status, field)) for field in _STABLE_FIELDS)


def _directory_identity(status: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_uid),
    )


def read_bounded(path: Path, *, maximum_bytes: int, private: bool = False) -> bytes:
    if not isinstance(path, Path) or maximum_bytes < 1:
        raise RecallContractError("input path contract is invalid")
    lexical = Path(os.path.abspath(path))
    descriptor = -1
    try:
        if lexical.resolve(strict=True) != lexical:
            raise RecallContractError("benchmark input must be a lexical regular file")
        lexical_before = os.stat(lexical, follow_symlinks=False)
        descriptor = os.open(
            lexical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            _stable_identity(lexical_before) != _stable_identity(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or (private and stat.S_IMODE(before.st_mode) & 0o077)
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise RecallContractError("benchmark input is not an owner-safe regular file")
        chunks = bytearray()
        while len(chunks) <= maximum_bytes:
            chunk = os.read(descriptor, min(1 << 20, maximum_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        lexical_after = os.stat(lexical, follow_symlinks=False)
        if (
            len(chunks) > maximum_bytes
            or len(chunks) != before.st_size
            or _stable_identity(before) != _stable_identity(after)
            or _stable_identity(before) != _stable_identity(lexical_after)
        ):
            raise RecallContractError("benchmark input changed while it was read")
    except OSError as exc:
        raise RecallContractError("benchmark input is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return bytes(chunks)


def _parse_jsonl(
    value: bytes,
    parser: Callable[[bytes], ContractT],
    *,
    label: str,
) -> tuple[ContractT, ...]:
    if type(value) is not bytes or not value or len(value) > MAX_JSONL_BYTES:
        raise RecallContractError(f"{label} JSONL exceeds its closed byte bound")
    if b"\r" in value or not value.endswith(b"\n"):
        raise RecallContractError(f"{label} JSONL must use canonical LF records")
    lines = value[:-1].split(b"\n")
    if (
        not lines
        or len(lines) > MAX_JSONL_ITEMS
        or any(not line or len(line) > MAX_JSONL_LINE_BYTES for line in lines)
    ):
        raise RecallContractError(f"{label} JSONL exceeds its closed record bound")
    return tuple(parser(line) for line in lines)


def parse_cases_jsonl(value: bytes) -> tuple[RecallCaseV1, ...]:
    cases = _parse_jsonl(value, RecallCaseV1.parse, label="case")
    ids = tuple(item.case_id for item in cases)
    if len(ids) != len(set(ids)):
        raise RecallContractError("case JSONL contains duplicate IDs")
    keys = tuple(item.privacy_key_hex for item in cases)
    if len(keys) != len(set(keys)):
        raise RecallContractError("case JSONL privacy keys must be unique")
    return cases


def parse_observations_jsonl(value: bytes) -> tuple[RecallObservationV1, ...]:
    observations = _parse_jsonl(value, RecallObservationV1.parse, label="observation")
    ids = tuple(item.case_id for item in observations)
    if len(ids) != len(set(ids)):
        raise RecallContractError("observation JSONL contains duplicate IDs")
    return observations


def read_cases(path: Path) -> tuple[RecallCaseV1, ...]:
    return parse_cases_jsonl(read_bounded(path, maximum_bytes=MAX_JSONL_BYTES, private=True))


def read_observations(path: Path) -> tuple[RecallObservationV1, ...]:
    return parse_observations_jsonl(read_bounded(path, maximum_bytes=MAX_JSONL_BYTES))


def read_report(path: Path) -> RecallReportV1:
    value = read_bounded(path, maximum_bytes=MAX_REPORT_BYTES)
    if value.endswith(b"\n"):
        value = value[:-1]
    if not value or b"\r" in value or b"\n" in value:
        raise RecallContractError("report must contain one canonical JSON record")
    return RecallReportV1.parse(value)


def _materialize_outputs(
    outputs: Iterable[tuple[Path, bytes]],
    *,
    bounded: bool,
) -> tuple[tuple[Path, bytes], ...]:
    try:
        maximum_items = MAX_OUTPUT_ITEMS if bounded else 1
        values = tuple(islice(iter(outputs), maximum_items + 1))
    except Exception as exc:
        raise RecallContractError("benchmark output contract is invalid") from exc
    if not values or len(values) > maximum_items:
        raise RecallContractError("benchmark output contract exceeds its item bound")

    normalized: list[tuple[Path, bytes]] = []
    seen: set[Path] = set()
    total_bytes = 0
    for item in values:
        if type(item) is not tuple or len(item) != 2:
            raise RecallContractError("benchmark output contract is invalid")
        path, value = item
        if not isinstance(path, Path) or type(value) is not bytes or not value:
            raise RecallContractError("benchmark output contract is invalid")
        if bounded and len(value) > MAX_OUTPUT_ITEM_BYTES:
            raise RecallContractError("benchmark output contract exceeds its byte bound")
        total_bytes += len(value)
        lexical = Path(os.path.abspath(path))
        if lexical in seen:
            raise RecallContractError("benchmark output destinations must be unique")
        seen.add(lexical)
        normalized.append((lexical, value))
    if bounded and total_bytes > MAX_OUTPUT_TOTAL_BYTES:
        raise RecallContractError("benchmark output contract exceeds its byte bound")
    return tuple(normalized)


def _require_directory_binding(
    directory: Path,
    descriptor: int,
    expected: tuple[int, int, int, int],
) -> None:
    opened = os.fstat(descriptor)
    current = os.stat(directory, follow_symlinks=False)
    if _directory_identity(opened) != expected or _directory_identity(current) != expected:
        raise RecallContractError("benchmark output parent changed during publication")


def _require_missing(name: str, descriptor: int) -> None:
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RecallContractError("benchmark output destination already exists")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _publish_new_many(
    outputs: Iterable[tuple[Path, bytes]],
    *,
    bounded: bool,
) -> None:
    values = _materialize_outputs(outputs, bounded=bounded)
    directory_descriptors: dict[Path, int] = {}
    directory_identities: dict[Path, tuple[int, int, int, int]] = {}
    temporary_descriptors: dict[Path, int] = {}
    temporary_names: dict[Path, str] = {}
    written_statuses: dict[Path, os.stat_result] = {}
    published: list[Path] = []
    temporary_present: set[Path] = set()

    def rollback() -> bool:
        failed = False
        for lexical in reversed(published):
            descriptor = directory_descriptors[lexical.parent]
            expected = written_statuses.get(lexical)
            try:
                current = os.stat(lexical.name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
                continue
            if expected is not None and _same_inode(current, expected):
                try:
                    os.unlink(lexical.name, dir_fd=descriptor)
                except OSError:
                    failed = True
        for lexical in tuple(temporary_present):
            descriptor = directory_descriptors[lexical.parent]
            try:
                os.unlink(temporary_names[lexical], dir_fd=descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                failed = True
            temporary_present.discard(lexical)
        for descriptor in directory_descriptors.values():
            try:
                os.fsync(descriptor)
            except OSError:
                failed = True
        return failed

    try:
        for lexical, _value in values:
            directory = lexical.parent
            if directory in directory_descriptors:
                continue
            if directory.resolve(strict=True) != directory:
                raise RecallContractError("benchmark output parent must be a lexical directory")
            descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            directory_descriptors[directory] = descriptor
            directory_status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(directory_status.st_mode)
                or directory_status.st_uid != os.geteuid()
                or stat.S_IMODE(directory_status.st_mode) & 0o022
            ):
                raise RecallContractError("benchmark output parent is not owner-safe")
            identity = _directory_identity(directory_status)
            directory_identities[directory] = identity
            _require_directory_binding(directory, descriptor, identity)

        for lexical, _value in values:
            _require_directory_binding(
                lexical.parent,
                directory_descriptors[lexical.parent],
                directory_identities[lexical.parent],
            )
            _require_missing(lexical.name, directory_descriptors[lexical.parent])

        for lexical, value in values:
            descriptor = directory_descriptors[lexical.parent]
            temporary_name = f".{lexical.name}.friday-{secrets.token_hex(16)}.tmp"
            temporary_names[lexical] = temporary_name
            temporary_descriptor = os.open(
                temporary_name,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                _PRIVATE_FILE_MODE,
                dir_fd=descriptor,
            )
            temporary_descriptors[lexical] = temporary_descriptor
            temporary_present.add(lexical)
            os.fchmod(temporary_descriptor, _PRIVATE_FILE_MODE)
            remaining = memoryview(value)
            while remaining:
                written = os.write(temporary_descriptor, remaining)
                if written <= 0:  # pragma: no cover - operating-system contract guard
                    raise OSError("short benchmark sidecar write")
                remaining = remaining[written:]
            os.fsync(temporary_descriptor)
            written_status = os.fstat(temporary_descriptor)
            if (
                not stat.S_ISREG(written_status.st_mode)
                or written_status.st_nlink != 1
                or written_status.st_uid != os.geteuid()
                or stat.S_IMODE(written_status.st_mode) != _PRIVATE_FILE_MODE
                or written_status.st_size != len(value)
            ):
                raise RecallContractError("benchmark output is not a sealed private file")
            written_statuses[lexical] = written_status

        for directory, descriptor in directory_descriptors.items():
            _require_directory_binding(directory, descriptor, directory_identities[directory])
        for lexical, _value in values:
            _require_missing(lexical.name, directory_descriptors[lexical.parent])

        for lexical, _value in values:
            descriptor = directory_descriptors[lexical.parent]
            expected = written_statuses[lexical]
            temporary = os.stat(
                temporary_names[lexical],
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if not _same_inode(temporary, expected) or temporary.st_nlink != 1:
                raise RecallContractError("benchmark output temporary changed before publication")
            published.append(lexical)
            os.link(
                temporary_names[lexical],
                lexical.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
            final = os.stat(lexical.name, dir_fd=descriptor, follow_symlinks=False)
            if not _same_inode(final, expected) or final.st_nlink != 2:
                raise RecallContractError("benchmark output publication identity changed")

        for lexical, _value in values:
            descriptor = directory_descriptors[lexical.parent]
            os.unlink(temporary_names[lexical], dir_fd=descriptor)
            temporary_present.remove(lexical)

        for lexical, value in values:
            descriptor = directory_descriptors[lexical.parent]
            expected = written_statuses[lexical]
            final = os.stat(lexical.name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not _same_inode(final, expected)
                or not stat.S_ISREG(final.st_mode)
                or final.st_nlink != 1
                or final.st_uid != os.geteuid()
                or stat.S_IMODE(final.st_mode) != _PRIVATE_FILE_MODE
                or final.st_size != len(value)
            ):
                raise RecallContractError("benchmark output is not a sealed private file")
        for directory, descriptor in directory_descriptors.items():
            _require_directory_binding(directory, descriptor, directory_identities[directory])
            os.fsync(descriptor)
    except Exception as exc:
        rollback_failed = rollback()
        if rollback_failed:
            raise RecallContractError("benchmark output rollback failed") from exc
        if isinstance(exc, RecallContractError):
            raise
        raise RecallContractError("benchmark output could not be created") from exc
    finally:
        for descriptor in temporary_descriptors.values():
            os.close(descriptor)
        for descriptor in directory_descriptors.values():
            os.close(descriptor)


def write_new(path: Path, value: bytes) -> None:
    """Atomically publish one private sidecar without following or replacing links."""

    _publish_new_many(((path, value),), bounded=False)


def write_new_many(outputs: Iterable[tuple[Path, bytes]]) -> None:
    """Publish a preflighted bounded group, rolling back caught failures."""

    _publish_new_many(outputs, bounded=True)


__all__ = [
    "MAX_JSONL_BYTES",
    "MAX_JSONL_ITEMS",
    "MAX_OUTPUT_ITEMS",
    "MAX_REPORT_BYTES",
    "parse_cases_jsonl",
    "parse_observations_jsonl",
    "read_cases",
    "read_observations",
    "read_report",
    "write_new",
    "write_new_many",
]
