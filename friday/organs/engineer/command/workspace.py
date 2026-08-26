"""Private per-job workspace. Generated files are admitted only from output/."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .contracts import (
    FORBIDDEN_EXACT_PATHS,
    FORBIDDEN_PATH_PREFIXES,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    CommandError,
    GeneratedFile,
    sha256_bytes,
)
from .store import atomic_write


def _is_forbidden(path: str) -> bool:
    if path in FORBIDDEN_EXACT_PATHS:
        return True
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in FORBIDDEN_PATH_PREFIXES)


class JobWorkspace:
    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.home = job_dir / "workspace"
        self.tmp = job_dir / "tmp"
        self.output = job_dir / "output"
        self.stdout_path = job_dir / "stdout.bin"
        self.stderr_path = job_dir / "stderr.bin"
        self.stdin_path = job_dir / "stdin.bin"

    def materialize(self, *, stdin: bytes) -> None:
        for path in (self.home, self.tmp, self.output):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        atomic_write(self.stdin_path, stdin)
        atomic_write(self.stdout_path, b"")
        atomic_write(self.stderr_path, b"")

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PWD": str(self.job_dir),
            "TMPDIR": str(self.tmp),
            "TZ": "UTC",
        }

    def admit_generated_files(self) -> tuple[GeneratedFile, ...]:
        if not self.output.exists():
            return ()
        admitted: list[GeneratedFile] = []
        total = 0
        for current, dirnames, filenames in os.walk(self.output, followlinks=False):
            dirnames[:] = sorted(dirnames)
            for name in sorted(filenames):
                path = Path(current) / name
                relative = path.relative_to(self.output).as_posix()
                if relative.startswith("../") or "/../" in f"/{relative}/":
                    raise CommandError("path_escape")
                try:
                    st = path.lstat()
                except OSError as exc:
                    raise CommandError("output_unreadable") from exc
                if stat.S_ISLNK(st.st_mode):
                    raise CommandError("output_symlink_refused")
                if not stat.S_ISREG(st.st_mode):
                    raise CommandError("output_not_regular")
                if st.st_nlink > 1:
                    raise CommandError("output_hardlink_refused")
                try:
                    path.relative_to(self.output)
                except ValueError as exc:
                    raise CommandError("path_escape") from exc
                if _is_forbidden(str(path)):
                    raise CommandError("forbidden_path")
                if st.st_size > MAX_OUTPUT_FILE_BYTES:
                    raise CommandError("output_file_too_large")
                total += st.st_size
                if total > MAX_OUTPUT_TREE_BYTES or len(admitted) >= MAX_OUTPUT_FILES:
                    raise CommandError("output_tree_overflow")
                digest = sha256_bytes(path.read_bytes())
                admitted.append(
                    GeneratedFile(
                        relative_path=relative,
                        size_bytes=int(st.st_size),
                        sha256=digest,
                        mode=int(stat.S_IMODE(st.st_mode)),
                    )
                )
        return tuple(admitted)
