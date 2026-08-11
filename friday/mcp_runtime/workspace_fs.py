"""Code-owned MCP filesystem exchange with narrow read/create boundaries.

The inbox is read-only.  The outbox is create-only: no read, overwrite, append,
rename or delete operation exists in the protocol.  Every path component is
opened with ``O_NOFOLLOW`` and the Friday host re-opens inbox files after MCP
selection, so a path race cannot redirect a read outside the configured root.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_MAX_SCAN_ENTRIES = 5_000
_MAX_QUERY_CHARS = 200
_PRIVATE_FILE_MODE = 0o600


class WorkspacePathError(ValueError):
    """A requested operation does not stay inside the configured exchange."""


@dataclass(frozen=True)
class WorkspaceFileDescriptor:
    relative_path: str
    filename: str
    size_bytes: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int


def _root_path(root: Path) -> Path:
    candidate = Path(root).expanduser().absolute()
    try:
        descriptor = _open_absolute_directory(candidate)
    except OSError as exc:
        raise WorkspacePathError("workspace root is unavailable") from exc
    os.close(descriptor)
    return candidate


def _relative_parts(value: str, *, allow_empty: bool) -> tuple[str, ...]:
    raw = str(value or "")
    if not raw:
        if allow_empty:
            return ()
        raise WorkspacePathError("relative path is required")
    if "\x00" in raw or "\\" in raw:
        raise WorkspacePathError("invalid relative path")
    if raw.startswith("/"):
        raise WorkspacePathError("absolute paths are not allowed")
    parts = tuple(raw.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise WorkspacePathError("path traversal is not allowed")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise WorkspacePathError("path component is too long")
    return tuple(parts)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_absolute_directory(path: Path) -> int:
    """Open an absolute directory one no-follow component at a time."""

    candidate = Path(path).absolute()
    anchor = candidate.anchor
    if not anchor:
        raise OSError("workspace root must be absolute")
    if any(component in {".", ".."} for component in candidate.parts[1:]):
        raise OSError("workspace root cannot contain dot segments")
    current_fd = os.open(anchor, _directory_flags())
    try:
        for component in candidate.parts[1:]:
            next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        os.close(current_fd)
        raise


def _open_workspace_dir(root: Path, relative_dir: str = "") -> tuple[int, tuple[str, ...]]:
    safe_root = _root_path(root)
    parts = _relative_parts(relative_dir, allow_empty=True)
    current_fd = _open_absolute_directory(safe_root)
    try:
        for component in parts:
            next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts
    except OSError as exc:
        os.close(current_fd)
        raise WorkspacePathError("workspace directory is unavailable") from exc


def _open_workspace_file(root: Path, relative_path: str) -> tuple[int, os.stat_result]:
    """Open one independent regular file without following any symlink."""

    parts = _relative_parts(relative_path, allow_empty=False)
    parent = PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else ""
    directory_fd, _ = _open_workspace_dir(root, parent)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
    except OSError as exc:
        raise WorkspacePathError("workspace file is unavailable") from exc
    finally:
        os.close(directory_fd)
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(file_fd)
        raise WorkspacePathError("workspace entry is not an independent regular file")
    return file_fd, info


def describe_workspace_file(root: Path, relative_path: str) -> WorkspaceFileDescriptor:
    file_fd, info = _open_workspace_file(root, relative_path)
    os.close(file_fd)
    normalized = PurePosixPath(*_relative_parts(relative_path, allow_empty=False)).as_posix()
    return WorkspaceFileDescriptor(
        relative_path=normalized,
        filename=PurePosixPath(normalized).name,
        size_bytes=int(info.st_size),
        modified_ns=int(info.st_mtime_ns),
        changed_ns=int(info.st_ctime_ns),
        device=int(info.st_dev),
        inode=int(info.st_ino),
    )


def read_workspace_file(
    root: Path,
    descriptor: WorkspaceFileDescriptor,
    *,
    max_bytes: int,
) -> bytes:
    """Read exactly the selected inbox file, bounded and race-safe."""

    if max_bytes < 1 or descriptor.size_bytes < 0 or descriptor.size_bytes > max_bytes:
        raise WorkspacePathError("workspace file exceeds the configured byte limit")
    file_fd, info = _open_workspace_file(root, descriptor.relative_path)
    try:
        identity = (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )
        expected = (
            descriptor.device,
            descriptor.inode,
            descriptor.size_bytes,
            descriptor.modified_ns,
            descriptor.changed_ns,
        )
        if identity != expected:
            raise WorkspacePathError("workspace file changed while it was being selected")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        final_info = os.fstat(file_fd)
        final_identity = (
            int(final_info.st_dev),
            int(final_info.st_ino),
            int(final_info.st_size),
            int(final_info.st_mtime_ns),
            int(final_info.st_ctime_ns),
        )
        if len(content) > max_bytes or len(content) != descriptor.size_bytes or final_identity != expected:
            raise WorkspacePathError("workspace file changed while it was being read")
        return content
    finally:
        os.close(file_fd)


def _scan_entries(
    root: Path,
    relative_dir: str,
    *,
    recursive: bool,
) -> tuple[list[dict[str, Any]], bool]:
    base_parts = _relative_parts(relative_dir, allow_empty=True)
    # Resolve the requested base once before treating inaccessible descendants as
    # an incomplete listing rather than a protocol failure.
    base_fd, _ = _open_workspace_dir(root, relative_dir)
    os.close(base_fd)
    pending: list[tuple[str, ...]] = [base_parts]
    rows: list[dict[str, Any]] = []
    scanned = 0
    scan_complete = True
    try:
        while pending:
            prefix = pending.pop()
            prefix_text = PurePosixPath(*prefix).as_posix() if prefix else ""
            try:
                directory_fd, _ = _open_workspace_dir(root, prefix_text)
            except WorkspacePathError:
                scan_complete = False
                continue
            try:
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        scanned += 1
                        if scanned > _MAX_SCAN_ENTRIES:
                            scan_complete = False
                            return rows, scan_complete
                        try:
                            if entry.is_symlink():
                                continue
                            info = entry.stat(follow_symlinks=False)
                            is_dir = stat.S_ISDIR(info.st_mode)
                            is_file = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        except OSError:
                            scan_complete = False
                            continue
                        if not (is_dir or is_file):
                            continue
                        relative_parts = (*prefix, entry.name)
                        relative = PurePosixPath(*relative_parts).as_posix()
                        rows.append(
                            {
                                "path": relative,
                                "name": entry.name,
                                "type": "directory" if is_dir else "file",
                                "size_bytes": 0 if is_dir else int(info.st_size),
                                "modified_ns": int(info.st_mtime_ns),
                            }
                        )
                        if recursive and is_dir:
                            pending.append(relative_parts)
            finally:
                os.close(directory_fd)
    finally:
        pending.clear()
    rows.sort(key=lambda item: str(item["path"]).casefold())
    return rows, scan_complete


def list_workspace_entries(
    root: Path,
    *,
    relative_dir: str = "",
    recursive: bool = False,
    cursor: int = 0,
    limit: int = _DEFAULT_PAGE_SIZE,
    query: str = "",
) -> dict[str, Any]:
    offset = max(0, min(int(cursor), _MAX_SCAN_ENTRIES))
    page_size = max(1, min(int(limit), _MAX_PAGE_SIZE))
    needle = " ".join(str(query or "").casefold().split())
    if len(needle) > _MAX_QUERY_CHARS:
        raise WorkspacePathError("search query is too long")
    scanned_rows, scan_complete = _scan_entries(root, relative_dir, recursive=recursive)
    matching = [row for row in scanned_rows if not needle or needle in str(row["path"]).casefold()]
    page = matching[offset : offset + page_size]
    has_more = len(matching) > offset + len(page)
    complete = scan_complete and not has_more
    next_cursor = offset + len(page) if has_more else None
    return {
        "entries": page,
        "returned": len(page),
        "matched_at_least": len(matching),
        "complete": complete,
        "scan_limit_reached": not scan_complete,
        "next_cursor": next_cursor,
        "page_limit": page_size,
    }


def create_workspace_file(
    root: Path,
    *,
    filename: str,
    content_base64: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Atomically create one new private outbox file without replacement."""

    parts = _relative_parts(filename, allow_empty=False)
    if len(parts) != 1:
        raise WorkspacePathError("outbox accepts a filename, not a path")
    if max_bytes < 1:
        raise WorkspacePathError("invalid output byte limit")
    encoded = str(content_base64 or "")
    max_encoded = 4 * ((max_bytes + 2) // 3)
    if len(encoded) > max_encoded:
        raise WorkspacePathError("workspace output exceeds the configured byte limit")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkspacePathError("workspace output is not valid base64") from exc
    if len(content) > max_bytes:
        raise WorkspacePathError("workspace output exceeds the configured byte limit")

    directory_fd, _ = _open_workspace_dir(root)
    temporary_name = f".friday-{secrets.token_hex(16)}.tmp"
    temporary_fd: int | None = None
    linked = False
    try:
        flags = (
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_fd = os.open(temporary_name, flags, _PRIVATE_FILE_MODE, dir_fd=directory_fd)
        view = memoryview(content)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:  # pragma: no cover - OS contract guard
                raise OSError("short workspace write")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.link(
            temporary_name,
            parts[0],
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise WorkspacePathError("workspace output already exists") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if not linked:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)
    return {
        "created": True,
        "filename": parts[0],
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def create_workspace_server(inbox_root: Path, outbox_root: Path, *, max_output_bytes: int):
    """Build the fixed MCP server; no dynamic capabilities are accepted."""

    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    safe_inbox = _root_path(inbox_root)
    safe_outbox = _root_path(outbox_root)
    if (
        safe_inbox.samefile(safe_outbox)
        or safe_inbox in safe_outbox.parents
        or safe_outbox in safe_inbox.parents
    ):
        raise WorkspacePathError("workspace inbox and outbox must be different directories")
    server = MCPServer(
        "friday-workspace",
        description="Read-only inbox and create-only outbox configured by Friday.",
    )
    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    create_only = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )

    @server.tool(name="exchange_list", annotations=read_only, structured_output=True)
    def exchange_list(
        relative_dir: str = "",
        recursive: bool = False,
        cursor: int = 0,
        limit: int = _DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """List bounded metadata from the read-only inbox."""

        return list_workspace_entries(
            safe_inbox,
            relative_dir=relative_dir,
            recursive=recursive,
            cursor=cursor,
            limit=limit,
        )

    @server.tool(name="exchange_search", annotations=read_only, structured_output=True)
    def exchange_search(
        query: str,
        relative_dir: str = "",
        cursor: int = 0,
        limit: int = _DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Search bounded filenames under the read-only inbox."""

        if not " ".join(str(query or "").split()):
            raise WorkspacePathError("search query is required")
        return list_workspace_entries(
            safe_inbox,
            relative_dir=relative_dir,
            recursive=True,
            cursor=cursor,
            limit=limit,
            query=query,
        )

    @server.tool(name="exchange_resolve", annotations=read_only, structured_output=True)
    def exchange_resolve(relative_path: str) -> dict[str, Any]:
        """Return a short-lived identity for one independent inbox file."""

        return asdict(describe_workspace_file(safe_inbox, relative_path))

    @server.tool(name="exchange_create", annotations=create_only, structured_output=True)
    def exchange_create(filename: str, content_base64: str) -> dict[str, Any]:
        """Atomically create one new outbox file; never overwrite an entry."""

        return create_workspace_file(
            safe_outbox,
            filename=filename,
            content_base64=content_base64,
            max_bytes=max_output_bytes,
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Friday filesystem exchange MCP server")
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--max-output-bytes", required=True, type=int)
    args = parser.parse_args(argv)
    create_workspace_server(
        Path(args.inbox),
        Path(args.outbox),
        max_output_bytes=max(1, int(args.max_output_bytes)),
    ).run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a stdio subprocess
    raise SystemExit(main())
