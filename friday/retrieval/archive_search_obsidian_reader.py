"""Owner-bound exact-byte reads for authorized Obsidian archive hits.

The adapter deliberately accepts an already composed
``ObsidianOperationService``.  It never resolves a caller supplied server path
or constructs a filesystem service of its own.  ``read_note`` remains the
canonical path/symlink/size boundary; this layer only pins that read to the
trusted owner and vault identities and verifies the returned UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import NoReturn, SupportsIndex

from friday.organs.obsidian.contracts import NoteDocument, validate_revision
from friday.organs.obsidian.operations import ObsidianOperationService
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.storage import FridayStorage

MAX_ARCHIVE_OBSIDIAN_EXACT_READ_BYTES = 4 * 1024 * 1024

_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)


class ArchiveObsidianExactReadError(RuntimeError):
    """Body-free rejection at the owner-bound exact-file read seam."""


def _fail() -> ArchiveObsidianExactReadError:
    return ArchiveObsidianExactReadError("archive Obsidian exact read failed")


def _identity(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail() from None
    if len(encoded) > 200:
        raise _fail()
    return value


def _composition(
    *,
    service: ObsidianOperationService,
    storage: FridayStorage,
    notes: ObsidianService,
    store: VaultStore,
    owner_id: str,
    vault_id: str,
) -> tuple[object, ...]:
    try:
        root_identity = store._root_identity  # noqa: SLF001 - exact trusted composition proof
        convention = notes.convention
        if (
            type(service) is not ObsidianOperationService
            or type(storage) is not FridayStorage
            or type(notes) is not ObsidianService
            or type(store) is not VaultStore
            or service._storage is not storage  # noqa: SLF001
            or service._notes is not notes  # noqa: SLF001
            or notes.store is not store
            or service.owner_id != owner_id
            or service.vault_id != vault_id
            or type(root_identity) is not tuple
            or len(root_identity) != 2
            or any(type(item) is not int or item < 0 for item in root_identity)
        ):
            raise _fail()
        return (
            id(service),
            id(storage),
            id(notes),
            id(store),
            owner_id,
            vault_id,
            service._folder_id,  # noqa: SLF001
            str(store.root),
            root_identity,
            convention.daily_folder,
            convention.daily_format,
            convention.template_folder,
            convention.attachment_folder,
        )
    except ArchiveObsidianExactReadError:
        raise
    except Exception:
        raise _fail() from None


def _seal(
    *,
    service: ObsidianOperationService,
    storage: FridayStorage,
    notes: ObsidianService,
    store: VaultStore,
    owner_id: str,
    vault_id: str,
) -> bytes:
    material = json.dumps(
        _composition(
            service=service,
            storage=storage,
            notes=notes,
            store=store,
            owner_id=owner_id,
            vault_id=vault_id,
        ),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hmac.new(
        _PROCESS_KEY,
        b"friday/archive-obsidian-exact-reader/v1\0" + material,
        hashlib.sha256,
    ).digest()


class BoundArchiveObsidianExactFileReader:
    """Process-private callable bound to one trusted owner/vault service.

    The canonical service rechecks its durable owner-to-vault binding before
    every read.  Its vault store returns one coherent, revisioned byte read;
    the pathname can still change immediately afterwards because SQLite and
    the synchronized filesystem do not share a lock.  Archive publication
    therefore has to perform its separately required second-phase read.
    """

    __slots__ = (
        "_notes",
        "_owner_id",
        "_seal",
        "_service",
        "_storage",
        "_store",
        "_vault_id",
    )

    def __init__(
        self,
        service: ObsidianOperationService,
        *,
        owner_id: str,
        vault_id: str,
        _factory: object,
    ) -> None:
        if _factory is not _FACTORY or type(service) is not ObsidianOperationService:
            raise _fail()
        owner = _identity(owner_id)
        vault = _identity(vault_id)
        try:
            if service.owner_id != owner or service.vault_id != vault:
                raise _fail()
        except ArchiveObsidianExactReadError:
            raise
        except Exception:
            raise _fail() from None
        try:
            storage = service._storage  # noqa: SLF001 - frozen trusted composition
            notes = service._notes  # noqa: SLF001 - frozen trusted composition
            store = notes.store
            if (
                type(storage) is not FridayStorage
                or type(notes) is not ObsidianService
                or type(store) is not VaultStore
            ):
                raise _fail()
        except ArchiveObsidianExactReadError:
            raise
        except Exception:
            raise _fail() from None
        self._service = service
        self._storage = storage
        self._notes = notes
        self._store = store
        self._owner_id = owner
        self._vault_id = vault
        self._seal = _seal(
            service=service,
            storage=storage,
            notes=notes,
            store=store,
            owner_id=owner,
            vault_id=vault,
        )

    def __repr__(self) -> str:
        return "<BoundArchiveObsidianExactFileReader sealed private>"

    def __copy__(self) -> NoReturn:
        raise TypeError("archive Obsidian exact reader is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive Obsidian exact reader is process-private")

    def __reduce__(self) -> NoReturn:
        raise TypeError("archive Obsidian exact reader is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive Obsidian exact reader is process-private")

    def attests_owner(self, owner_id: object) -> bool:
        """Attest this exact live composition and its process-bound owner."""

        try:
            owner = _identity(owner_id)
            return bool(
                type(self) is BoundArchiveObsidianExactFileReader
                and type(self._service) is ObsidianOperationService
                and type(self._storage) is FridayStorage
                and type(self._notes) is ObsidianService
                and type(self._store) is VaultStore
                and hmac.compare_digest(owner, self._owner_id)
                and hmac.compare_digest(
                    self._seal,
                    _seal(
                        service=self._service,
                        storage=self._storage,
                        notes=self._notes,
                        store=self._store,
                        owner_id=self._owner_id,
                        vault_id=self._vault_id,
                    ),
                )
            )
        except Exception:
            return False

    def __call__(
        self,
        vault_id: str,
        path: str,
        expected_sha256: str,
        /,
    ) -> bytes:
        """Return only the exact revision bytes or raise a body-free error."""

        try:
            service = self._service
            storage = self._storage
            notes = self._notes
            store = self._store
            owner = self._owner_id
            vault = self._vault_id
            if (
                type(service) is not ObsidianOperationService
                or type(storage) is not FridayStorage
                or type(notes) is not ObsidianService
                or type(store) is not VaultStore
                or type(self._seal) is not bytes
                or not hmac.compare_digest(
                    self._seal,
                    _seal(
                        service=service,
                        storage=storage,
                        notes=notes,
                        store=store,
                        owner_id=owner,
                        vault_id=vault,
                    ),
                )
                or service.owner_id != owner
                or service.vault_id != vault
                or type(vault_id) is not str
                or not hmac.compare_digest(vault_id, vault)
                or type(path) is not str
                or not path
                or type(expected_sha256) is not str
            ):
                raise _fail()
            expected = validate_revision(expected_sha256)
            document = service.read_note(path)
            if (
                type(document) is not NoteDocument
                or document.path != path
                or document.revision != expected
                or type(document.content) is not str
                or type(document.size_bytes) is not int
                or isinstance(document.size_bytes, bool)
                or document.size_bytes < 0
                or len(document.content) > MAX_ARCHIVE_OBSIDIAN_EXACT_READ_BYTES
            ):
                raise _fail()
            encoded = document.content.encode("utf-8", errors="strict")
            if (
                len(encoded) > MAX_ARCHIVE_OBSIDIAN_EXACT_READ_BYTES
                or len(encoded) != document.size_bytes
                or not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), expected)
                or service.owner_id != owner
                or service.vault_id != vault
                or not hmac.compare_digest(
                    self._seal,
                    _seal(
                        service=service,
                        storage=storage,
                        notes=notes,
                        store=store,
                        owner_id=owner,
                        vault_id=vault,
                    ),
                )
            ):
                raise _fail()
            return encoded
        except ArchiveObsidianExactReadError:
            raise
        except Exception:
            raise _fail() from None


def bind_archive_obsidian_exact_file_reader(
    service: ObsidianOperationService,
    *,
    owner_id: str,
    vault_id: str,
) -> BoundArchiveObsidianExactFileReader:
    """Bind a trusted operation service; identities must come from composition."""

    return BoundArchiveObsidianExactFileReader(
        service,
        owner_id=owner_id,
        vault_id=vault_id,
        _factory=_FACTORY,
    )


__all__ = [
    "ArchiveObsidianExactReadError",
    "BoundArchiveObsidianExactFileReader",
    "MAX_ARCHIVE_OBSIDIAN_EXACT_READ_BYTES",
    "bind_archive_obsidian_exact_file_reader",
]
