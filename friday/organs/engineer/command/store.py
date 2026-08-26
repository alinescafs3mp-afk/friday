"""Durable per-job evidence. Isolated from production host-agent job tables."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .contracts import SCHEMA, CommandError, canonical_json_bytes


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(tmp), flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    atomic_write(path, canonical_json_bytes(payload) + b"\n", mode=mode)


def read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("ascii"))
    except FileNotFoundError as exc:
        raise CommandError("job_not_found") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError("corrupt_job_state") from exc
    if not isinstance(data, dict):
        raise CommandError("corrupt_job_state")
    return data


class CommandJobStore:
    """Filesystem job ledger. Never opens production host-agent job tables."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs = self.root / "jobs"
        self._jobs.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "idempotency.json"
        if not self._index_path.exists():
            atomic_write_json(self._index_path, {"schema": SCHEMA, "entries": {}})

    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or job_id.startswith("."):
            raise CommandError("invalid_job_id")
        return self._jobs / job_id

    def load_index(self) -> dict[str, Any]:
        payload = read_json(self._index_path)
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise CommandError("corrupt_job_state")
        return entries

    def remember_idempotency(self, actor_id: str, key: str, job_id: str, digest: str) -> None:
        entries = self.load_index()
        entries[f"{actor_id}\0{key}"] = {"job_id": job_id, "digest": digest}
        atomic_write_json(self._index_path, {"schema": SCHEMA, "entries": entries})

    def lookup_idempotency(self, actor_id: str, key: str) -> dict[str, str] | None:
        entries = self.load_index()
        found = entries.get(f"{actor_id}\0{key}")
        if found is None:
            return None
        if not isinstance(found, dict) or "job_id" not in found or "digest" not in found:
            raise CommandError("corrupt_job_state")
        return {"job_id": str(found["job_id"]), "digest": str(found["digest"])}

    def write_state(self, job_id: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["schema"] = SCHEMA
        atomic_write_json(self.job_dir(job_id) / "state.json", payload)

    def read_state(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "state.json")
