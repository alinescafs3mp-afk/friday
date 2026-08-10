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
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from friday.private_fs import (
    ensure_private_directory,
    restrict_private_file,
    write_private_text_if_missing,
)

LOGGER = logging.getLogger(__name__)
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Filenames may keep any letter — the user's titles are Russian, and the ASCII-only
# encoder above would flatten every one of them to the same empty slug. Only what
# Windows actually forbids is removed: the reserved punctuation and control codes.
# The `--<digest>` suffix keeps reserved device names (CON, PRN, ...) unreachable.
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


# NAME_MAX is 255 BYTES, not characters, and the slug is the only part of a
# filename a person controls. A note is written through `tempfile.mkstemp` with
# `prefix=".<stem>."` and `suffix=".tmp"`, so the name that actually has to fit is
# the slug plus 28 bytes — and `mkstemp` raises OSError before the try block that
# would clean up after it. 60 characters of an astral-plane title (emoji,
# mathematical bold) is 240 bytes on its own, so a title a user can produce by
# accident made every later object in the vault unwritable.
_SLUG_BYTE_BUDGET = 200


class VaultAccountWriteBlocked(RuntimeError):
    """The durable account tombstone forbids recreating its file projection."""


def _clip_bytes(value: str, limit: int) -> str:
    """Clip to a UTF-8 byte budget without splitting a character in half."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _safe_component(value: str, *, fallback: str = "unknown") -> str:
    """Return a cross-platform directory component without losing identity."""
    original = (value or fallback).strip()
    slug = _SAFE_COMPONENT_RE.sub("-", original).strip(" .-")[:48]
    slug = _clip_bytes(slug, _SLUG_BYTE_BUDGET).strip(" .-") or fallback
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

    def __init__(
        self,
        vault_dir: Path,
        *,
        account_is_deleted: Callable[[str], bool] | None = None,
    ) -> None:
        self._vault_dir = Path(vault_dir).resolve()
        self._users_dir = self._vault_dir / "users"
        self._account_is_deleted = account_is_deleted
        ensure_private_directory(self._vault_dir)
        ensure_private_directory(self._users_dir)

    def _assert_account_writable(self, user_id: str) -> None:
        if self._account_is_deleted is None:
            return
        try:
            deleted = bool(self._account_is_deleted(user_id))
        except Exception as exc:
            raise VaultAccountWriteBlocked(
                "Account deletion state could not be verified; vault write refused"
            ) from exc
        if deleted:
            raise VaultAccountWriteBlocked("Permanently deleted account cannot be synced")

    def _user_dir(self, user_id: str) -> Path:
        return self._users_dir / _safe_component(user_id)

    @staticmethod
    def _note_stem(ko_id: str) -> str:
        """The stable half of a note's filename: identity, never the title.

        A note is named `<title-slug>--<id-digest>.md`. The digest half is what
        `delete_object` and `prune_orphans` match on, so retitling an object renames
        its file without breaking either — and two objects that happen to share a
        title never collide.
        """
        return _safe_component(ko_id, fallback="knowledge").rsplit("--", 1)[-1]

    def _note_path(self, user_dir: Path, ko: dict[str, Any]) -> Path:
        ko_id = str(ko.get("id") or "")
        title = str(ko.get("title") or "").strip()
        slug = _UNSAFE_FILENAME_RE.sub("-", title)
        slug = re.sub(r"\s+", " ", slug).strip(" .-")[:60]
        # `[:60]` is the readability limit; the byte clip is the correctness one.
        slug = _clip_bytes(slug, _SLUG_BYTE_BUDGET).strip(" .-")
        return user_dir / f"{slug or 'без-названия'}--{self._note_stem(ko_id)}.md"

    @staticmethod
    def _ensure_readme(user_dir: Path, user_id: str) -> None:
        """Name the tenant, because the directory name cannot.

        The folder is `<slug>--<digest>` for multi-tenant safety, which tells a
        person opening the vault nothing at all about whose knowledge they are
        looking at.
        """
        readme = user_dir / "README.md"
        if readme.exists():
            restrict_private_file(readme)
            return
        with suppress(OSError):
            write_private_text_if_missing(
                readme,
                f"# Хранилище знаний Friday\n\n"
                f"Аккаунт: `{user_id}`\n\n"
                "Это **проекция**: источник истины — SQLite внутри Friday. Правки в этих\n"
                "файлах будут перезаписаны при следующей синхронизации; правьте знания\n"
                "в самом Friday.\n\n"
                "Заметка называется `<заголовок>--<идентификатор>.md`. Раздел «Связи»\n"
                "содержит `[[ссылки]]` на сущности — по ним и строится граф.\n",
                encoding="utf-8",
            )

    def _existing_notes(self, user_dir: Path, ko_id: str) -> list[Path]:
        if not user_dir.is_dir():
            return []
        return sorted(user_dir.glob(f"*--{self._note_stem(ko_id)}.md"))

    def sync_object(self, ko: dict[str, Any]) -> Path | None:
        """Write or update one knowledge object with an atomic replace."""
        ko_id = str(ko.get("id") or "").strip()
        user_id = str(ko.get("user_id") or "").strip()
        if not ko_id or not user_id:
            return None

        self._assert_account_writable(user_id)
        content = self._render_markdown(ko)
        self._assert_account_writable(user_id)
        user_dir = self._user_dir(user_id)
        ensure_private_directory(user_dir)
        self._assert_account_writable(user_id)
        self._ensure_readme(user_dir, user_id)
        self._assert_account_writable(user_id)
        filepath = self._note_path(user_dir, ko)

        # An unchanged note is not rewritten. The sync loop renders the WHOLE
        # corpus every five minutes, and mkstemp + fsync + replace for every
        # untouched note meant ~442 000 fsyncs and gigabytes of parasitic writes
        # a day on a corpus nobody edited (1537 objects × 288 cycles) — on the
        # single disk that also holds the database. Reading the file back is
        # page-cache cheap, and equality means there is nothing to do at all:
        # a retitled object lands on a DIFFERENT filename (the title is part of
        # it), so the comparison never masks a rename.
        if filepath.exists():
            with suppress(OSError, UnicodeDecodeError):
                if filepath.read_text(encoding="utf-8") == content:
                    return filepath

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{filepath.stem}.", suffix=".tmp", dir=str(user_dir)
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_account_writable(user_id)
            os.replace(temp_path, filepath)
            # A retitled object gets a new name; without this its old file stays
            # behind as a stale twin that read_vault would return alongside it.
            for stale in self._existing_notes(user_dir, ko_id):
                if stale != filepath:
                    with suppress(OSError):
                        stale.unlink()
        finally:
            temp_path.unlink(missing_ok=True)
        return filepath

    def delete_object(self, ko_id: str, user_id: str) -> None:
        """Remove the Markdown projection; the database keeps deletion history.

        By id-digest, not by exact name: the title is part of the filename now, and
        deleting by a reconstructed name would miss a note whose title had changed.
        """
        for path in self._existing_notes(self._user_dir(user_id), ko_id):
            with suppress(OSError):
                path.unlink()

    def prune_orphans(self, user_id: str, live_ko_ids: Iterable[str]) -> int:
        """Drop projections of objects that are no longer live. Returns the count.

        The vault is a projection, and a projection that only ever gains rows is
        not one. ``sync_object`` wrote every live object and nothing removed a file
        when an object stopped being live: ``list_knowledge_objects`` filters
        ``deleted_at IS NULL``, and ``delete_object`` had exactly one production
        caller — the hard-purge path. So a soft-deleted Knowledge Object, or one the
        reviewer marked IGNORED, kept a **plaintext Markdown copy of its full
        content on disk forever**, while the user was told it had been deleted and
        the search agreed.

        Compared by filename rather than by parsing each file: ``sync_object``
        derives the name from the id through the same encoder, so the mapping is
        exact and no file has to be opened to decide its fate.
        """
        user_dir = self._user_dir(user_id)
        if not user_dir.is_dir():
            return 0
        # Matched on the id digest, because the title half of the name changes.
        expected = {self._note_stem(str(ko_id)) for ko_id in live_ko_ids}
        removed = 0
        for path in sorted(user_dir.glob("*.md")):
            if path.name == "README.md" or path.is_symlink():
                continue
            if path.stem.rsplit("--", 1)[-1] in expected:
                continue
            with suppress(OSError):
                path.unlink()
                removed += 1
        return removed

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
                if md_file.name == "README.md":
                    continue
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
                    LOGGER.warning("Failed to read vault file (%s)", type(exc).__name__)
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
        # Wikilinks, so the vault is a graph in Obsidian rather than a flat pile.
        # Two notes about the same project meet at that project's node — which is
        # the whole point of opening the vault outside Friday, and it was missing:
        # `list_knowledge_entity_links` existed and the vault never called it.
        # Unresolved links are fine here: Obsidian shows them in the graph, and the
        # entity note is created the moment the owner clicks one.
        entities = [str(name).strip() for name in (ko.get("_entity_names") or []) if str(name).strip()]
        if entities:
            unique = list(dict.fromkeys(entities))
            lines.extend(["", "## Связи", ""])
            lines.extend(f"- [[{name}]]" for name in unique)
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
