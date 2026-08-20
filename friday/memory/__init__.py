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
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

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
_FINAL_NOTE_RE = re.compile(r"^.+--(?P<digest>[0-9a-f]{12})\.md$")
_ATOMIC_TEMP_RE = re.compile(r"^\..+--(?P<digest>[0-9a-f]{12})\.[A-Za-z0-9_-]+\.tmp$")


class VaultAccountWriteBlocked(RuntimeError):
    """The durable account tombstone forbids recreating its file projection."""


class VaultProjectionBoundaryError(OSError):
    """A vault projection could not be inspected or changed without escaping its root."""


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


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _note_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _descriptor_projection_supported() -> bool:
    """Whether Python exposes the no-follow, descriptor-relative boundary we require."""

    return bool(
        getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def _projection_artifact_digest(name: str) -> str | None:
    for pattern in (_FINAL_NOTE_RE, _ATOMIC_TEMP_RE):
        match = pattern.fullmatch(name)
        if match is not None:
            return str(match.group("digest"))
    return None


class MemoryVaultDeletionHandle:
    """Deletion-only, non-creating access to an optional or legacy projection.

    Every traversal walks the absolute configured root component-by-component
    with ``O_NOFOLLOW`` and then opens ``users`` and the encoded account directory
    relative to pinned descriptors. A directory symlink therefore cannot redirect
    a purge, prune, or read outside the configured lexical boundary. Unsupported
    platforms fail closed rather than claiming a deletion or inventory result.
    """

    def __init__(self, vault_dir: Path) -> None:
        self._vault_dir = Path(os.path.abspath(vault_dir))
        self._users_dir = self._vault_dir / "users"

    def _open_vault_descriptor(self, *, create: bool = False) -> int | None:
        """Walk every configured component relative to a pinned parent descriptor."""

        if not _descriptor_projection_supported():
            raise VaultProjectionBoundaryError("descriptor-relative vault traversal is unavailable")
        anchor = Path(self._vault_dir.anchor)
        components = self._vault_dir.parts[1:]
        if not anchor.is_absolute() or not components:
            raise VaultProjectionBoundaryError("vault root must be a non-root absolute path")

        descriptor = -1
        child_descriptor = -1
        try:
            descriptor = os.open(anchor, _directory_open_flags())
            for index, component in enumerate(components):
                child_descriptor = -1
                try:
                    child_descriptor = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create or index != len(components) - 1:
                        os.close(descriptor)
                        return None
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                        child_descriptor = os.open(
                            component,
                            _directory_open_flags(),
                            dir_fd=descriptor,
                        )
                    except (OSError, TypeError, NotImplementedError) as exc:
                        raise VaultProjectionBoundaryError("vault root could not be created safely") from exc
                opened_status = os.fstat(child_descriptor)
                lexical_status = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(opened_status.st_mode) or not _same_file_identity(
                    opened_status,
                    lexical_status,
                ):
                    raise VaultProjectionBoundaryError("vault root component changed during traversal")
                os.close(descriptor)
                descriptor = child_descriptor
                child_descriptor = -1
            return descriptor
        except VaultProjectionBoundaryError:
            if child_descriptor >= 0:
                os.close(child_descriptor)
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            if child_descriptor >= 0:
                os.close(child_descriptor)
            if descriptor >= 0:
                os.close(descriptor)
            raise VaultProjectionBoundaryError("vault root traversal was refused") from exc

    def _open_users_descriptor(self) -> int | None:
        vault_descriptor = self._open_vault_descriptor()
        if vault_descriptor is None:
            return None
        users_descriptor = -1
        keep_users_descriptor = False
        try:
            try:
                users_descriptor = os.open(
                    "users",
                    _directory_open_flags(),
                    dir_fd=vault_descriptor,
                )
            except FileNotFoundError:
                return None
            opened_status = os.fstat(users_descriptor)
            lexical_status = os.stat("users", dir_fd=vault_descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(opened_status.st_mode) or not _same_file_identity(
                opened_status,
                lexical_status,
            ):
                raise VaultProjectionBoundaryError("vault users directory changed during traversal")
            keep_users_descriptor = True
            return users_descriptor
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault users traversal was refused") from exc
        finally:
            os.close(vault_descriptor)
            if users_descriptor >= 0 and not keep_users_descriptor:
                os.close(users_descriptor)

    def create_vault_structure(self) -> None:
        """Create only the vault leaf and ``users`` below pinned parent descriptors."""

        vault_descriptor = self._open_vault_descriptor(create=True)
        if vault_descriptor is None:
            raise VaultProjectionBoundaryError("vault parent directory does not exist")
        users_descriptor = -1
        try:
            os.fchmod(vault_descriptor, 0o700)
            try:
                os.mkdir("users", mode=0o700, dir_fd=vault_descriptor)
                os.fsync(vault_descriptor)
            except FileExistsError:
                pass
            users_descriptor = os.open(
                "users",
                _directory_open_flags(),
                dir_fd=vault_descriptor,
            )
            opened_status = os.fstat(users_descriptor)
            lexical_status = os.stat(
                "users",
                dir_fd=vault_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(opened_status.st_mode) or not _same_file_identity(
                opened_status,
                lexical_status,
            ):
                raise VaultProjectionBoundaryError("vault users directory is not safely pinned")
            os.fchmod(users_descriptor, 0o700)
            os.fsync(users_descriptor)
            os.fsync(vault_descriptor)
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault structure creation was refused") from exc
        finally:
            if users_descriptor >= 0:
                os.close(users_descriptor)
            os.close(vault_descriptor)

    @staticmethod
    def _assert_account_descriptor(
        users_descriptor: int,
        account_descriptor: int,
        account_name: str,
    ) -> None:
        try:
            opened_status = os.fstat(account_descriptor)
            lexical_status = os.stat(
                account_name,
                dir_fd=users_descriptor,
                follow_symlinks=False,
            )
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault account directory changed during traversal") from exc
        if not stat.S_ISDIR(opened_status.st_mode) or not _same_file_identity(
            opened_status,
            lexical_status,
        ):
            raise VaultProjectionBoundaryError("vault account directory changed during traversal")

    @staticmethod
    def _open_account_descriptor(users_descriptor: int, account_name: str) -> int | None:
        try:
            descriptor = os.open(account_name, _directory_open_flags(), dir_fd=users_descriptor)
        except FileNotFoundError:
            return None
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault account directory traversal was refused") from exc
        try:
            MemoryVaultDeletionHandle._assert_account_descriptor(
                users_descriptor,
                descriptor,
                account_name,
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _account_descriptor(self, user_id: str) -> tuple[int, int] | None:
        users_descriptor = self._open_users_descriptor()
        if users_descriptor is None:
            return None
        try:
            account_descriptor = self._open_account_descriptor(
                users_descriptor,
                _safe_component(user_id),
            )
        except BaseException:
            os.close(users_descriptor)
            raise
        if account_descriptor is None:
            os.close(users_descriptor)
            return None
        return users_descriptor, account_descriptor

    def create_account_descriptor(self, user_id: str) -> tuple[int, int]:
        """Create/open one account through the already verified ``users`` descriptor."""

        users_descriptor = self._open_users_descriptor()
        if users_descriptor is None:
            raise VaultProjectionBoundaryError("vault users directory disappeared")
        account_name = _safe_component(user_id)
        try:
            try:
                os.mkdir(account_name, mode=0o700, dir_fd=users_descriptor)
                os.fsync(users_descriptor)
            except FileExistsError:
                pass
            except (OSError, TypeError, NotImplementedError) as exc:
                raise VaultProjectionBoundaryError(
                    "vault account directory could not be created safely"
                ) from exc
            account_descriptor = self._open_account_descriptor(users_descriptor, account_name)
            if account_descriptor is None:
                raise VaultProjectionBoundaryError("vault account directory disappeared")
            try:
                os.fchmod(account_descriptor, 0o700)
            except OSError as exc:
                os.close(account_descriptor)
                raise VaultProjectionBoundaryError(
                    "vault account permissions could not be confirmed"
                ) from exc
            return users_descriptor, account_descriptor
        except BaseException:
            os.close(users_descriptor)
            raise

    @staticmethod
    def _matching_regular_names(
        account_descriptor: int,
        predicate: Callable[[str], bool],
    ) -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(account_descriptor) as entries:
                for entry in entries:
                    name = entry.name
                    if not predicate(name):
                        continue
                    try:
                        status = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise VaultProjectionBoundaryError("vault note changed during inspection") from exc
                    if entry.is_symlink() or not stat.S_ISREG(status.st_mode):
                        raise VaultProjectionBoundaryError("vault note is not a real regular file")
                    names.append(name)
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault account could not be enumerated safely") from exc
        return sorted(names)

    @staticmethod
    def _unlink_regular_name(account_descriptor: int, name: str) -> None:
        note_descriptor = -1
        try:
            note_descriptor = os.open(name, _note_open_flags(), dir_fd=account_descriptor)
            opened_status = os.fstat(note_descriptor)
            lexical_status = os.stat(
                name,
                dir_fd=account_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(opened_status.st_mode) or not _same_file_identity(
                opened_status,
                lexical_status,
            ):
                raise VaultProjectionBoundaryError("vault note changed before deletion")
            os.unlink(name, dir_fd=account_descriptor)
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault note deletion was not confirmed") from exc
        finally:
            if note_descriptor >= 0:
                os.close(note_descriptor)

    def _delete_matching(self, user_id: str, predicate: Callable[[str], bool]) -> int:
        descriptors = self._account_descriptor(user_id)
        if descriptors is None:
            return 0
        users_descriptor, account_descriptor = descriptors
        try:
            return self.delete_matching_from_descriptor(
                users_descriptor,
                account_descriptor,
                _safe_component(user_id),
                predicate,
            )
        finally:
            os.close(account_descriptor)
            os.close(users_descriptor)

    def delete_matching_from_descriptor(
        self,
        users_descriptor: int,
        account_descriptor: int,
        account_name: str,
        predicate: Callable[[str], bool],
    ) -> int:
        """Delete through one pinned account descriptor and durably confirm its state."""

        self._assert_account_descriptor(users_descriptor, account_descriptor, account_name)
        names = self._matching_regular_names(account_descriptor, predicate)
        removed = 0
        for name in names:
            self._assert_account_descriptor(users_descriptor, account_descriptor, account_name)
            self._unlink_regular_name(account_descriptor, name)
            removed += 1
        try:
            # A successful hard-purge receipt must survive power loss.  Fsync even
            # for an empty match: this also makes a retry after a prior fsync error
            # durably confirm the already-performed unlink before the DB commit.
            os.fsync(account_descriptor)
        except OSError as exc:
            raise VaultProjectionBoundaryError("vault note deletion durability was not confirmed") from exc
        self._assert_account_descriptor(users_descriptor, account_descriptor, account_name)
        return removed

    def delete_object(self, ko_id: str, user_id: str) -> int:
        """Remove matching final/crash-temp files and return confirmed unlink count."""

        digest = MemoryVault._note_stem(ko_id)
        return self._delete_matching(
            user_id,
            lambda name: _projection_artifact_digest(name) == digest,
        )

    def delete_stale_twins(self, ko_id: str, user_id: str, keep_name: str) -> int:
        digest = MemoryVault._note_stem(ko_id)
        return self._delete_matching(
            user_id,
            lambda name: name != keep_name and _projection_artifact_digest(name) == digest,
        )

    def prune_orphans(self, user_id: str, live_ko_ids: Iterable[str]) -> int:
        expected = {MemoryVault._note_stem(str(ko_id)) for ko_id in live_ko_ids}
        return self._delete_matching(
            user_id,
            lambda name: (
                (digest := _projection_artifact_digest(name)) is not None
                and (name.endswith(".tmp") or digest not in expected)
            ),
        )

    def account_state(self, user_id: str) -> str:
        """Return absent/empty/material without following or opening account entries."""

        descriptors = self._account_descriptor(user_id)
        if descriptors is None:
            return "absent"
        users_descriptor, account_descriptor = descriptors
        try:
            self._assert_account_descriptor(
                users_descriptor,
                account_descriptor,
                _safe_component(user_id),
            )
            with os.scandir(account_descriptor) as entries:
                return "material" if next(entries, None) is not None else "empty"
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault account state was not safely inspectable") from exc
        finally:
            os.close(account_descriptor)
            os.close(users_descriptor)

    def remove_empty_account(self, user_id: str) -> bool:
        """Durably remove one confirmed-empty account leaf through the users descriptor."""

        users_descriptor = self._open_users_descriptor()
        if users_descriptor is None:
            return False
        account_name = _safe_component(user_id)
        account_descriptor = self._open_account_descriptor(users_descriptor, account_name)
        if account_descriptor is None:
            os.close(users_descriptor)
            return False
        try:
            self._assert_account_descriptor(users_descriptor, account_descriptor, account_name)
            with os.scandir(account_descriptor) as entries:
                if next(entries, None) is not None:
                    return False
            os.rmdir(account_name, dir_fd=users_descriptor)
            os.fsync(users_descriptor)
            return True
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault account removal was not confirmed") from exc
        finally:
            os.close(account_descriptor)
            os.close(users_descriptor)

    def inventory_projection(self) -> tuple[bool, int, bool, bool]:
        """Return root-present, note-count, any-artifact, scan-complete without bodies."""

        vault_descriptor = self._open_vault_descriptor()
        if vault_descriptor is None:
            return False, 0, False, True
        os.close(vault_descriptor)
        users_descriptor = self._open_users_descriptor()
        if users_descriptor is None:
            return True, 0, False, True
        count = 0
        artifact_present = False
        scan_complete = True
        try:
            with os.scandir(users_descriptor) as accounts:
                account_entries = list(accounts)
            for account in account_entries:
                if account.is_symlink():
                    scan_complete = False
                    continue
                if account.is_file(follow_symlinks=False):
                    artifact_present = True
                    if account.name.endswith(".md") and account.name != "README.md":
                        count += 1
                    continue
                if not account.is_dir(follow_symlinks=False):
                    scan_complete = False
                    continue
                try:
                    account_descriptor = self._open_account_descriptor(
                        users_descriptor,
                        account.name,
                    )
                except VaultProjectionBoundaryError:
                    scan_complete = False
                    continue
                if account_descriptor is None:
                    scan_complete = False
                    continue
                try:
                    with os.scandir(account_descriptor) as notes:
                        for note in notes:
                            if note.is_symlink():
                                scan_complete = False
                                continue
                            if note.is_file(follow_symlinks=False):
                                artifact_present = True
                                if note.name.endswith(".md") and note.name != "README.md":
                                    count += 1
                            else:
                                scan_complete = False
                finally:
                    os.close(account_descriptor)
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault projection inventory was refused") from exc
        finally:
            os.close(users_descriptor)
        return True, count, artifact_present, scan_complete

    @staticmethod
    def _read_regular_note(account_descriptor: int, name: str) -> str:
        descriptor = -1
        try:
            descriptor = os.open(name, _note_open_flags(), dir_fd=account_descriptor)
            opened_status = os.fstat(descriptor)
            lexical_status = os.stat(
                name,
                dir_fd=account_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(opened_status.st_mode) or not _same_file_identity(
                opened_status,
                lexical_status,
            ):
                raise VaultProjectionBoundaryError("vault note changed before read")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                return handle.read()
        except VaultProjectionBoundaryError:
            raise
        except (OSError, UnicodeError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault note read was refused") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def read_note(self, user_id: str, name: str) -> str | None:
        descriptors = self._account_descriptor(user_id)
        if descriptors is None:
            return None
        users_descriptor, account_descriptor = descriptors
        try:
            names = self._matching_regular_names(account_descriptor, lambda item: item == name)
            if not names:
                return None
            return self._read_regular_note(account_descriptor, names[0])
        finally:
            os.close(account_descriptor)
            os.close(users_descriptor)

    def read_notes(self, user_id: str | None = None) -> list[tuple[str, str, str]]:
        users_descriptor = self._open_users_descriptor()
        if users_descriptor is None:
            return []
        account_names: list[str]
        if user_id is not None:
            account_names = [_safe_component(user_id)]
        else:
            try:
                with os.scandir(users_descriptor) as entries:
                    account_names = sorted(entry.name for entry in entries if not entry.is_symlink())
            except (OSError, TypeError, NotImplementedError) as exc:
                os.close(users_descriptor)
                raise VaultProjectionBoundaryError("vault users could not be enumerated safely") from exc
        result: list[tuple[str, str, str]] = []
        try:
            for account_name in account_names:
                try:
                    account_descriptor = self._open_account_descriptor(users_descriptor, account_name)
                except VaultProjectionBoundaryError:
                    continue
                if account_descriptor is None:
                    continue
                try:
                    names = self._matching_regular_names(
                        account_descriptor,
                        lambda name: name.endswith(".md") and name != "README.md",
                    )
                    for name in names:
                        result.append((account_name, name, self._read_regular_note(account_descriptor, name)))
                finally:
                    os.close(account_descriptor)
        finally:
            os.close(users_descriptor)
        return result


class MemoryVault:
    """Atomically mirror knowledge objects as Markdown files."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        account_is_deleted: Callable[[str], bool] | None = None,
    ) -> None:
        if not _descriptor_projection_supported():
            raise VaultProjectionBoundaryError(
                "full_owner memory vault requires descriptor-relative no-follow filesystem support"
            )
        # Keep the configured lexical root: resolving it here would bless a
        # symlink target as the vault itself before the private-directory guard
        # has a chance to reject the redirect.
        self._vault_dir = Path(os.path.abspath(vault_dir))
        self._users_dir = self._vault_dir / "users"
        self._account_is_deleted = account_is_deleted
        self._deletion_handle = MemoryVaultDeletionHandle(self._vault_dir)
        self._deletion_handle.create_vault_structure()

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
    def _ensure_readme(account_descriptor: int, user_id: str) -> None:
        """Name the tenant, because the directory name cannot.

        The folder is `<slug>--<digest>` for multi-tenant safety, which tells a
        person opening the vault nothing at all about whose knowledge they are
        looking at.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            try:
                descriptor = os.open("README.md", flags, 0o600, dir_fd=account_descriptor)
            except FileExistsError:
                descriptor = os.open("README.md", _note_open_flags(), dir_fd=account_descriptor)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise VaultProjectionBoundaryError("vault README is not a regular file") from None
                os.fchmod(descriptor, 0o600)
                return
            body = (
                f"# Хранилище знаний Friday\n\n"
                f"Аккаунт: `{user_id}`\n\n"
                "Это **проекция**: источник истины — SQLite внутри Friday. Правки в этих\n"
                "файлах будут перезаписаны при следующей синхронизации; правьте знания\n"
                "в самом Friday.\n\n"
                "Заметка называется `<заголовок>--<идентификатор>.md`. Раздел «Связи»\n"
                "содержит `[[ссылки]]` на сущности — по ним и строится граф.\n"
            ).encode()
            offset = 0
            while offset < len(body):
                offset += os.write(descriptor, body[offset:])
            os.fsync(descriptor)
            os.fsync(account_descriptor)
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault README could not be created safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _create_temp_note(account_descriptor: int, stem: str) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(32):
            name = f".{stem}.{os.urandom(8).hex()}.tmp"
            try:
                return os.open(name, flags, 0o600, dir_fd=account_descriptor), name
            except FileExistsError:
                continue
            except (OSError, TypeError, NotImplementedError) as exc:
                raise VaultProjectionBoundaryError(
                    "vault temporary note could not be created safely"
                ) from exc
        raise VaultProjectionBoundaryError("vault temporary note name could not be allocated")

    @staticmethod
    def _ensure_private_note_permissions(account_descriptor: int, name: str) -> None:
        """Repair a full-owner note through its pinned regular-file descriptor."""

        descriptor = -1
        try:
            descriptor = os.open(name, _note_open_flags(), dir_fd=account_descriptor)
            opened_status = os.fstat(descriptor)
            lexical_status = os.stat(
                name,
                dir_fd=account_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(opened_status.st_mode) or not _same_file_identity(
                opened_status,
                lexical_status,
            ):
                raise VaultProjectionBoundaryError("vault note changed before permission repair")
            if stat.S_IMODE(opened_status.st_mode) == 0o600:
                return
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            confirmed_status = os.fstat(descriptor)
            lexical_status = os.stat(
                name,
                dir_fd=account_descriptor,
                follow_symlinks=False,
            )
            if stat.S_IMODE(confirmed_status.st_mode) != 0o600 or not _same_file_identity(
                confirmed_status, lexical_status
            ):
                raise VaultProjectionBoundaryError("vault note permissions could not be confirmed")
        except VaultProjectionBoundaryError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise VaultProjectionBoundaryError("vault note permission repair was refused") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

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
        users_descriptor, account_descriptor = self._deletion_handle.create_account_descriptor(user_id)
        account_name = user_dir.name
        filepath = self._note_path(user_dir, ko)
        try:
            self._assert_account_writable(user_id)
            self._deletion_handle._assert_account_descriptor(  # noqa: SLF001
                users_descriptor,
                account_descriptor,
                account_name,
            )
            self._ensure_readme(account_descriptor, user_id)
            self._assert_account_writable(user_id)

            # An unchanged canonical note is not rewritten. The sync loop renders
            # the WHOLE corpus every five minutes, and fsync + replace for every
            # untouched note causes substantial parasitic writes on the database
            # disk. Still enumerate every artifact for this KO: a crash after the
            # replace but before stale-twin cleanup must converge on the next pass.
            digest = self._note_stem(ko_id)
            projection_names = self._deletion_handle._matching_regular_names(  # noqa: SLF001
                account_descriptor,
                lambda name: _projection_artifact_digest(name) == digest,
            )
            existing_content = (
                self._deletion_handle._read_regular_note(  # noqa: SLF001
                    account_descriptor,
                    filepath.name,
                )
                if filepath.name in projection_names
                else None
            )
            if existing_content == content:
                self._ensure_private_note_permissions(account_descriptor, filepath.name)
                if len(projection_names) > 1:
                    self._deletion_handle.delete_matching_from_descriptor(
                        users_descriptor,
                        account_descriptor,
                        account_name,
                        lambda name: name != filepath.name and _projection_artifact_digest(name) == digest,
                    )
                return filepath

            descriptor, temp_name = self._create_temp_note(account_descriptor, filepath.stem)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    descriptor = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._assert_account_writable(user_id)
                self._deletion_handle._assert_account_descriptor(  # noqa: SLF001
                    users_descriptor,
                    account_descriptor,
                    account_name,
                )
                os.replace(
                    temp_name,
                    filepath.name,
                    src_dir_fd=account_descriptor,
                    dst_dir_fd=account_descriptor,
                )
                temp_name = ""
                # Retitles leave a stale twin; remove it through this same pinned
                # account descriptor and make both replace/unlinks durable.
                self._deletion_handle.delete_matching_from_descriptor(
                    users_descriptor,
                    account_descriptor,
                    account_name,
                    lambda name: name != filepath.name and _projection_artifact_digest(name) == digest,
                )
            except VaultProjectionBoundaryError:
                raise
            except (OSError, TypeError, NotImplementedError) as exc:
                raise VaultProjectionBoundaryError("vault note replace was refused") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temp_name:
                    try:
                        os.unlink(temp_name, dir_fd=account_descriptor)
                        os.fsync(account_descriptor)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(account_descriptor)
            os.close(users_descriptor)
        return filepath

    def delete_object(self, ko_id: str, user_id: str) -> int:
        """Remove the Markdown projection; the database keeps deletion history.

        By id-digest, not by exact name: the title is part of the filename now, and
        deleting by a reconstructed name would miss a note whose title had changed.
        """
        return self._deletion_handle.delete_object(ko_id, user_id)

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
        return self._deletion_handle.prune_orphans(user_id, live_ko_ids)

    def read_vault(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Read notes, optionally filtering by the original (not encoded) user ID."""
        notes: list[dict[str, Any]] = []
        try:
            records = self._deletion_handle.read_notes(user_id)
        except VaultProjectionBoundaryError as exc:
            LOGGER.warning("Failed to read vault projection (%s)", type(exc).__name__)
            return notes
        for account_name, note_name, content in records:
            try:
                frontmatter = self._parse_frontmatter(content)
                original_user_id = str(frontmatter.get("user_id") or "")
                if user_id and original_user_id != user_id:
                    continue
                notes.append(
                    {
                        "id": frontmatter.get("id") or Path(note_name).stem,
                        "path": str(self._users_dir / account_name / note_name),
                        "user_id": original_user_id,
                        **frontmatter,
                    }
                )
            except (UnicodeError, ValueError) as exc:
                LOGGER.warning("Failed to parse vault file (%s)", type(exc).__name__)
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


__all__ = [
    "MemoryVault",
    "MemoryVaultDeletionHandle",
    "VaultProjectionBoundaryError",
]
