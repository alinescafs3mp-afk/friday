"""Filesystem mirror for human-readable knowledge notes.

The SQLite database remains the source of truth.  The vault is an atomic,
portable Markdown projection that can be inspected, indexed, and backed up
with ordinary filesystem tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: str, *, fallback: str = "unknown") -> str:
    """Return a cross-platform directory component without losing identity."""
    original = (value or fallback).strip()
    slug = _SAFE_COMPONENT_RE.sub("-", original).strip(" .-")[:48] or fallback
    digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{slug}--{digest}"


def _yaml_scalar(value: Any) -> str:
    """JSON scalars are valid YAML and avoid frontmatter injection."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


class MemoryVault:
    """Atomically mirror knowledge objects as Markdown files."""

    def __init__(self, vault_dir: Path) -> None:
        self._vault_dir = Path(vault_dir).resolve()
        self._users_dir = self._vault_dir / "users"
        self._users_dir.mkdir(parents=True, exist_ok=True)

    def _user_dir(self, user_id: str) -> Path:
        return self._users_dir / _safe_component(user_id)

    def sync_object(self, ko: dict[str, Any]) -> Path | None:
        """Write or update one knowledge object with an atomic replace."""
        ko_id = str(ko.get("id") or "").strip()
        user_id = str(ko.get("user_id") or "").strip()
        if not ko_id or not user_id:
            return None

        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        filepath = user_dir / f"{_safe_component(ko_id, fallback='knowledge')}.md"
        content = self._render_markdown(ko)

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{filepath.stem}.", suffix=".tmp", dir=str(user_dir)
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, filepath)
        finally:
            temp_path.unlink(missing_ok=True)
        return filepath

    def delete_object(self, ko_id: str, user_id: str) -> None:
        """Remove the Markdown projection; the database keeps deletion history."""
        filepath = self._user_dir(user_id) / f"{_safe_component(ko_id, fallback='knowledge')}.md"
        filepath.unlink(missing_ok=True)

    def read_vault(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Read notes, optionally filtering by the original (not encoded) user ID."""
        notes: list[dict[str, Any]] = []
        if not self._users_dir.exists():
            return notes

        candidate_dirs = [self._user_dir(user_id)] if user_id else list(self._users_dir.iterdir())
        for user_dir in candidate_dirs:
            if not user_dir.is_dir():
                continue
            for md_file in user_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    frontmatter = self._parse_frontmatter(content)
                    original_user_id = str(frontmatter.get("user_id") or "")
                    if user_id and original_user_id != user_id:
                        continue
                    notes.append(
                        {
                            "id": frontmatter.get("id") or md_file.stem,
                            "path": str(md_file),
                            "user_id": original_user_id,
                            **frontmatter,
                        }
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    LOGGER.warning("Failed to read vault file %s: %s", md_file, exc)
        return notes

    def _render_markdown(self, ko: dict[str, Any]) -> str:
        tags_value = ko.get("tags_json", [])
        if isinstance(tags_value, str):
            try:
                tags_value = json.loads(tags_value)
            except json.JSONDecodeError:
                tags_value = []
        tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
        frontmatter = {
            "id": str(ko.get("id") or ""),
            "user_id": str(ko.get("user_id") or ""),
            "title": str(ko.get("title") or ""),
            "tags": tags,
            "importance": float(ko.get("importance") or 0.0),
            "lifecycle_stage": str(ko.get("lifecycle_stage") or "active"),
            "version": int(ko.get("version") or 1),
            "entity_id": str(ko.get("entity_id") or ""),
            "provenance_raw_object_id": str(ko.get("raw_object_id") or ""),
            "created_at": str(ko.get("created_at") or ""),
            "updated_at": str(ko.get("updated_at") or ""),
        }

        lines = ["---"]
        lines.extend(f"{key}: {_yaml_scalar(value)}" for key, value in frontmatter.items())
        lines.extend(["---", "", f"# {str(ko.get('title') or 'Без названия').strip()}", ""])
        summary = str(ko.get("summary") or "").strip()
        if summary:
            safe_summary = summary.replace("\n", "\n> ")
            lines.extend([f"> {safe_summary}", ""])
        lines.append(str(ko.get("content") or ""))
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not content.startswith("---\n"):
            return result
        end = content.find("\n---", 4)
        if end == -1:
            return result
        for raw_line in content[4:end].splitlines():
            if ":" not in raw_line:
                continue
            key, value = raw_line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = value
        return result


__all__ = ["MemoryVault"]
