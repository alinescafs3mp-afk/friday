"""Exact repository identities for P6 heuristic-retirement review.

The retirement inventory is code-owned.  Callers may select a full Git commit,
but they cannot nominate an arbitrary function or relabel an invariant as a
semantic heuristic.  Source is read from Git objects, never from the mutable
worktree, and accepted identities are sealed to the accepting process.

This module deliberately contains no deletion or release operation.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import hmac
import os
import re
import secrets
import selectors
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from friday.orchestration.supervisor_contracts import (
    TaskClass,
    canonical_dumps,
    canonical_sha256,
)

SUPERVISOR_RETIREMENT_REPOSITORY_SCHEMA = "friday.supervisor-retirement-repository.v3"
SUPERVISOR_RETIREMENT_CANDIDATE_SCHEMA = "friday.supervisor-retirement-candidate.v3"
SUPERVISOR_RETIREMENT_REGISTRY_SCHEMA = "friday.supervisor-retirement-registry.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_QUALIFIED_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 15.0
_MAX_DELETION_SCAN_FILES = 1_024
_MAX_DELETION_SCAN_BYTES = 32 * 1024 * 1024
_MAX_DELETION_SCAN_AST_NODES = 4_194_304

_PROCESS_AUTHORITY = object()
_PROCESS_SEAL_KEY = secrets.token_bytes(32)

_REPLACEMENT_POLICY_PATH = "friday/semantic_supervisor_policy.py"
_REPLACEMENT_MANIFEST_PATH = "friday/orchestration/capability_manifest.py"
_REPLACEMENT_ADAPTER_REGISTRY_PATH = "friday/orchestration/capability_binding.py"
_DOCUMENTATION_PATH = "outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_ROUTING_INVARIANT_AUDIT.md"
_STATUS_REGISTRY_PATH = "outer_sol/PROJECT_BACKLOG.md"
_RETIREMENT_REGISTRY_PATH = "friday/orchestration/supervisor_retirement_repository.py"
_RETIREMENT_REGISTRY_DIGEST_SYMBOL = "SUPERVISOR_RETIREMENT_REGISTRY_SHA256"
_RETIREMENT_REGISTRY_PAYLOAD_SYMBOL = "_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD"
_DELETION_SCAN_PREFIX = "friday/"
_DELETION_SCAN_SUFFIX = ".py"
_DELETION_SCAN_SCOPE = "friday/**/*.py"


class RetirementRepositoryError(ValueError):
    """The requested repository object is absent, mutable, or ineligible."""


class RetirementSurfaceClass(StrEnum):
    SEMANTIC_HEURISTIC = "semantic_heuristic"
    DETERMINISTIC_INVARIANT = "deterministic_invariant"
    AUTHORITY_GUARD = "authority_guard"
    LIFECYCLE_OR_STATE = "lifecycle_or_state"
    PUBLICATION_GUARD = "publication_guard"
    LEGACY_MIXED = "legacy_mixed"


class RetirementInventoryReason(StrEnum):
    ELIGIBLE_CANDIDATE_PRESENT = "eligible_candidate_present"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise RetirementRepositoryError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_oid(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _GIT_OID_RE.fullmatch(value) is None:
        raise RetirementRepositoryError(f"{label} must be a full lowercase Git object id")
    return value


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise RetirementRepositoryError(f"{label} is invalid")
    return value


def _repository_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise RetirementRepositoryError(f"{label} must be a canonical repository-relative path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise RetirementRepositoryError(f"{label} must be a canonical repository-relative path")
    if parsed.as_posix() != value:
        raise RetirementRepositoryError(f"{label} must be a canonical repository-relative path")
    return value


def _qualified_symbol(value: object) -> str:
    if not isinstance(value, str) or _QUALIFIED_SYMBOL_RE.fullmatch(value) is None:
        raise RetirementRepositoryError("qualified_symbol is invalid")
    return value


def _seal(kind: str, payload: dict[str, object]) -> str:
    envelope = canonical_dumps({"kind": kind, "payload": payload}).encode("utf-8")
    return hmac.new(_PROCESS_SEAL_KEY, envelope, hashlib.sha256).hexdigest()


def _run_git(
    repository_root: Path,
    *arguments: str,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "--no-replace-objects", "-C", str(repository_root), *arguments]
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed git executable and validated arguments
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise RetirementRepositoryError("Git repository inspection failed") from exc

    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    selector.register(process.stderr, selectors.EVENT_READ, stderr)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination: bytearray = key.data
                if len(destination) + len(chunk) > _MAX_GIT_OUTPUT_BYTES:
                    raise RetirementRepositoryError("Git repository inspection exceeded its byte budget")
                destination.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise RetirementRepositoryError("Git repository inspection failed") from exc
    except OSError as exc:
        raise RetirementRepositoryError("Git repository inspection failed") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
        process.stdout.close()
        process.stderr.close()

    result = subprocess.CompletedProcess(command, returncode, bytes(stdout), bytes(stderr))
    if result.returncode not in accepted_returncodes:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:240]
        suffix = f": {detail}" if detail else ""
        raise RetirementRepositoryError(f"Git repository inspection failed{suffix}")
    return result


def _exact_repository_root(value: str | Path) -> Path:
    try:
        requested = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RetirementRepositoryError("repository_root must name an existing directory") from exc
    if not requested.is_dir():
        raise RetirementRepositoryError("repository_root must name an existing directory")
    raw = _run_git(requested, "rev-parse", "--show-toplevel").stdout
    try:
        discovered = Path(raw.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise RetirementRepositoryError("Git returned an invalid repository root") from exc
    if discovered != requested:
        raise RetirementRepositoryError("repository_root must be the exact Git top level")
    return requested


def _single_ascii_line(raw: bytes, *, label: str) -> str:
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        raise RetirementRepositoryError(f"Git returned an invalid {label}") from exc
    if not value or "\n" in value or "\r" in value:
        raise RetirementRepositoryError(f"Git returned an invalid {label}")
    return value


@dataclass(frozen=True, slots=True)
class RepositoryRetirementSurface:
    """One code-reviewed surface; this descriptor grants no authority."""

    candidate_id: str
    journey: TaskClass
    surface_class: RetirementSurfaceClass
    source_path: str
    qualified_symbol: str

    def __post_init__(self) -> None:
        _safe_id(self.candidate_id, label="candidate_id")
        if not isinstance(self.journey, TaskClass):
            raise RetirementRepositoryError("journey must be a typed task class")
        if not isinstance(self.surface_class, RetirementSurfaceClass):
            raise RetirementRepositoryError("surface_class is invalid")
        _repository_path(self.source_path, label="source_path")
        _qualified_symbol(self.qualified_symbol)

    def payload(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "journey": self.journey.value,
            "surface_class": self.surface_class.value,
            "source_path": self.source_path,
            "qualified_symbol": self.qualified_symbol,
        }


_REGISTERED_SURFACES = (
    RepositoryRetirementSurface(
        candidate_id="current_file_web.request_preflight",
        journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        surface_class=RetirementSurfaceClass.DETERMINISTIC_INVARIANT,
        source_path="friday/orchestration/current_file_web_comparison.py",
        qualified_symbol="current_file_web_request_is_admitted",
    ),
    RepositoryRetirementSurface(
        candidate_id="legacy.absolute_reminder_outward_intent_guard",
        journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        surface_class=RetirementSurfaceClass.DETERMINISTIC_INVARIANT,
        source_path="friday/agent_runtime/__init__.py",
        qualified_symbol="_requires_outward_intent_arbiter",
    ),
    RepositoryRetirementSurface(
        candidate_id="legacy.attachment_web_query_arbiter",
        journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        surface_class=RetirementSurfaceClass.LEGACY_MIXED,
        source_path="friday/agent_runtime/__init__.py",
        qualified_symbol="AgentRuntime._attachment_web_query_by_arbiter",
    ),
    RepositoryRetirementSurface(
        candidate_id="legacy.shared_web_query_arbiter",
        journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        surface_class=RetirementSurfaceClass.LEGACY_MIXED,
        source_path="friday/agent_runtime/__init__.py",
        qualified_symbol="AgentRuntime._web_query_by_arbiter",
    ),
)
_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD = (
    {
        "candidate_id": "current_file_web.request_preflight",
        "journey": "compare_current_file_with_current_web",
        "surface_class": "deterministic_invariant",
        "source_path": "friday/orchestration/current_file_web_comparison.py",
        "qualified_symbol": "current_file_web_request_is_admitted",
    },
    {
        "candidate_id": "legacy.absolute_reminder_outward_intent_guard",
        "journey": "compare_current_file_with_current_web",
        "surface_class": "deterministic_invariant",
        "source_path": "friday/agent_runtime/__init__.py",
        "qualified_symbol": "_requires_outward_intent_arbiter",
    },
    {
        "candidate_id": "legacy.attachment_web_query_arbiter",
        "journey": "compare_current_file_with_current_web",
        "surface_class": "legacy_mixed",
        "source_path": "friday/agent_runtime/__init__.py",
        "qualified_symbol": "AgentRuntime._attachment_web_query_by_arbiter",
    },
    {
        "candidate_id": "legacy.shared_web_query_arbiter",
        "journey": "compare_current_file_with_current_web",
        "surface_class": "legacy_mixed",
        "source_path": "friday/agent_runtime/__init__.py",
        "qualified_symbol": "AgentRuntime._web_query_by_arbiter",
    },
)
_SURFACE_BY_ID = {surface.candidate_id: surface for surface in _REGISTERED_SURFACES}


def _retirement_registry_sha256(
    surfaces: tuple[RepositoryRetirementSurface, ...],
) -> str:
    return canonical_sha256(
        {
            "schema": SUPERVISOR_RETIREMENT_REGISTRY_SCHEMA,
            "surfaces": [surface.payload() for surface in surfaces],
        }
    )


# This reviewed literal is also required in every inspected Git commit.  A
# tuple/map edit without a matching literal and target-commit marker fails
# closed before any repository surface is accepted.
SUPERVISOR_RETIREMENT_REGISTRY_SHA256 = "47cd505ddd9599beefa0483875b11138ec933075322396aa680f5e25392fb36c"


def _require_process_registry() -> str:
    expected = _retirement_registry_sha256(_REGISTERED_SURFACES)
    declared = _digest(
        SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
        label="code-owned retirement registry digest",
    )
    expected_map = {surface.candidate_id: surface for surface in _REGISTERED_SURFACES}
    expected_payload = tuple(surface.payload() for surface in _REGISTERED_SURFACES)
    if (
        len(expected_map) != len(_REGISTERED_SURFACES)
        or expected_map != _SURFACE_BY_ID
        or expected_payload != _REVIEWED_RETIREMENT_REGISTRY_PAYLOAD
        or not hmac.compare_digest(expected, declared)
    ):
        raise RetirementRepositoryError("code-owned retirement registry identity mismatch")
    return expected


def _process_registry_is_current() -> bool:
    try:
        _require_process_registry()
    except RetirementRepositoryError:
        return False
    return True


def registered_retirement_surfaces() -> tuple[RepositoryRetirementSurface, ...]:
    """Return the closed, code-owned P6 inventory."""

    _require_process_registry()
    return _REGISTERED_SURFACES


@dataclass(frozen=True, slots=True)
class ExactRepositoryFile:
    """A regular file read from one exact commit/tree/blob identity."""

    source_commit: str
    source_tree_oid: str
    source_path: str
    file_mode: str
    blob_oid: str
    file_sha256: str
    byte_count: int
    _raw: bytes = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        payload = self.payload()
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or self.file_mode not in {"100644", "100755"}
            or type(self._raw) is not bytes
            or self.byte_count != len(self._raw)
            or hashlib.sha256(self._raw).hexdigest() != self.file_sha256
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(self._process_seal_sha256, _seal("repository-file", payload))
        ):
            raise RetirementRepositoryError("repository file was not accepted by this process")
        _git_oid(self.source_commit, label="source_commit")
        _git_oid(self.source_tree_oid, label="source_tree_oid")
        _git_oid(self.blob_oid, label="blob_oid")
        _repository_path(self.source_path, label="source_path")
        _digest(self.file_sha256, label="file_sha256")
        if self.byte_count < 0 or self.byte_count > _MAX_GIT_OUTPUT_BYTES:
            raise RetirementRepositoryError("repository file byte_count is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "source_commit": self.source_commit,
            "source_tree_oid": self.source_tree_oid,
            "source_path": self.source_path,
            "file_mode": self.file_mode,
            "blob_oid": self.blob_oid,
            "file_sha256": self.file_sha256,
            "byte_count": self.byte_count,
        }

    def raw_bytes(self) -> bytes:
        return self._raw


def accepted_repository_file_is_current(value: object) -> bool:
    if (
        type(value) is not ExactRepositoryFile
        or value._process_authority is not _PROCESS_AUTHORITY
        or type(value._raw) is not bytes
        or value.byte_count != len(value._raw)
        or not hmac.compare_digest(hashlib.sha256(value._raw).hexdigest(), value.file_sha256)
    ):
        return False
    expected = _seal("repository-file", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


class _RepositoryReader:
    def __init__(self, repository_root: str | Path) -> None:
        self.root = _exact_repository_root(repository_root)
        self._commits: dict[str, tuple[str, str]] = {}
        self._files: dict[tuple[str, str], ExactRepositoryFile] = {}
        self._modules: dict[tuple[str, str], ast.Module] = {}
        self._registries: dict[str, ExactRepositoryFile] = {}

    def commit(self, source_commit: str) -> tuple[str, str]:
        _git_oid(source_commit, label="source_commit")
        cached = self._commits.get(source_commit)
        if cached is not None:
            return cached
        resolved = _single_ascii_line(
            _run_git(self.root, "rev-parse", "--verify", f"{source_commit}^{{commit}}").stdout,
            label="commit id",
        )
        _git_oid(resolved, label="resolved commit")
        if not hmac.compare_digest(resolved, source_commit):
            raise RetirementRepositoryError("source_commit must be the exact full commit id")
        tree = _single_ascii_line(
            _run_git(self.root, "rev-parse", "--verify", f"{source_commit}^{{tree}}").stdout,
            label="tree id",
        )
        _git_oid(tree, label="source tree")
        result = (resolved, tree)
        self._commits[source_commit] = result
        return result

    def file(self, source_commit: str, source_path: str) -> ExactRepositoryFile:
        return self._file(source_commit, source_path, cache=True)

    def uncached_file(self, source_commit: str, source_path: str) -> ExactRepositoryFile:
        """Read one size-preflighted blob without retaining its body."""

        return self._file(source_commit, source_path, cache=False)

    def _file(
        self,
        source_commit: str,
        source_path: str,
        *,
        cache: bool,
    ) -> ExactRepositoryFile:
        source_path = _repository_path(source_path, label="source_path")
        commit, tree = self.commit(source_commit)
        cache_key = (commit, source_path)
        if cache:
            cached = self._files.get(cache_key)
            if cached is not None:
                return cached
        listing = _run_git(self.root, "ls-tree", commit, "--", source_path).stdout
        try:
            line = listing.decode("utf-8", errors="strict").rstrip("\n")
        except UnicodeError as exc:
            raise RetirementRepositoryError("Git returned an invalid tree entry") from exc
        if not line or "\n" in line or "\t" not in line:
            raise RetirementRepositoryError(f"repository path is absent: {source_path}")
        identity, listed_path = line.split("\t", 1)
        parts = identity.split(" ")
        if len(parts) != 3 or listed_path != source_path:
            raise RetirementRepositoryError("Git returned an ambiguous tree entry")
        mode, object_type, blob_oid = parts
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise RetirementRepositoryError("repository source must be a regular blob")
        _git_oid(blob_oid, label="blob_oid")
        raw_size = _single_ascii_line(
            _run_git(self.root, "cat-file", "-s", blob_oid).stdout,
            label="blob size",
        )
        if not raw_size.isdecimal():
            raise RetirementRepositoryError("Git returned an invalid blob size")
        blob_size = int(raw_size)
        if blob_size > _MAX_GIT_OUTPUT_BYTES:
            raise RetirementRepositoryError("repository source exceeds its byte budget")
        raw = _run_git(self.root, "cat-file", "blob", blob_oid).stdout
        if len(raw) != blob_size:
            raise RetirementRepositoryError("Git blob size changed during inspection")
        file_sha256 = hashlib.sha256(raw).hexdigest()
        payload: dict[str, object] = {
            "source_commit": commit,
            "source_tree_oid": tree,
            "source_path": source_path,
            "file_mode": mode,
            "blob_oid": blob_oid,
            "file_sha256": file_sha256,
            "byte_count": len(raw),
        }
        accepted = ExactRepositoryFile(
            source_commit=commit,
            source_tree_oid=tree,
            source_path=source_path,
            file_mode=mode,
            blob_oid=blob_oid,
            file_sha256=file_sha256,
            byte_count=len(raw),
            _raw=raw,
            _process_authority=_PROCESS_AUTHORITY,
            _process_seal_sha256=_seal("repository-file", payload),
        )
        if cache:
            self._files[cache_key] = accepted
        return accepted

    def module(self, source_file: ExactRepositoryFile) -> ast.Module:
        if not accepted_repository_file_is_current(source_file):
            raise RetirementRepositoryError("repository source was not accepted by this process")
        cache_key = (source_file.source_commit, source_file.source_path)
        cached = self._modules.get(cache_key)
        if cached is not None:
            return cached
        parsed = self._parse_module(source_file)
        self._modules[cache_key] = parsed
        return parsed

    def uncached_module(self, source_file: ExactRepositoryFile) -> ast.Module:
        """Parse one bounded source blob without retaining its AST in the reader."""

        if not accepted_repository_file_is_current(source_file):
            raise RetirementRepositoryError("repository source was not accepted by this process")
        return self._parse_module(source_file)

    @staticmethod
    def _parse_module(source_file: ExactRepositoryFile) -> ast.Module:
        try:
            source = source_file.raw_bytes().decode("utf-8", errors="strict")
            parsed = ast.parse(source, filename=source_file.source_path)
        except (SyntaxError, UnicodeError) as exc:
            raise RetirementRepositoryError("repository Python source is not parseable UTF-8") from exc
        return parsed

    def registry(self, source_commit: str) -> ExactRepositoryFile:
        """Require the exact commit to publish the reviewed registry digest."""

        commit, _ = self.commit(source_commit)
        cached = self._registries.get(commit)
        if cached is not None:
            return cached
        expected = _require_process_registry()
        registry_file = self.file(commit, _RETIREMENT_REGISTRY_PATH)
        declared, payload = _declared_registry_identity(self.module(registry_file))
        if not hmac.compare_digest(declared, expected):
            raise RetirementRepositoryError(
                "target commit retirement registry digest does not match reviewed code"
            )
        if payload != _REVIEWED_RETIREMENT_REGISTRY_PAYLOAD:
            raise RetirementRepositoryError(
                "target commit retirement registry payload does not match reviewed code"
            )
        self._registries[commit] = registry_file
        return registry_file

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor, _ = self.commit(ancestor)
        descendant, _ = self.commit(descendant)
        result = _run_git(
            self.root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            accepted_returncodes=frozenset({0, 1}),
        )
        return result.returncode == 0


def _declared_literal(module: ast.Module, *, symbol: str, label: str) -> object:
    matches: list[ast.AST | None] = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if symbol in names:
                matches.append(
                    node.value
                    if len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == symbol
                    else None
                )
        elif (
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol
        ):
            matches.append(node.value)
    if len(matches) != 1 or matches[0] is None:
        raise RetirementRepositoryError(f"target commit must expose one literal {label}")
    try:
        return ast.literal_eval(matches[0])
    except (TypeError, ValueError) as exc:
        raise RetirementRepositoryError(f"target commit must expose one literal {label}") from exc


def _declared_registry_identity(module: ast.Module) -> tuple[str, tuple[dict[str, str], ...]]:
    digest = _digest(
        _declared_literal(
            module,
            symbol=_RETIREMENT_REGISTRY_DIGEST_SYMBOL,
            label="retirement registry digest",
        ),
        label="target commit retirement registry digest",
    )
    raw_payload = _declared_literal(
        module,
        symbol=_RETIREMENT_REGISTRY_PAYLOAD_SYMBOL,
        label="retirement registry payload",
    )
    if type(raw_payload) is not tuple or not all(
        type(item) is dict
        and set(item)
        == {
            "candidate_id",
            "journey",
            "surface_class",
            "source_path",
            "qualified_symbol",
        }
        and all(type(key) is str and type(value) is str for key, value in item.items())
        for item in raw_payload
    ):
        raise RetirementRepositoryError("target commit retirement registry payload is invalid")
    return digest, raw_payload


def read_exact_repository_file(
    repository_root: str | Path,
    *,
    source_commit: str,
    source_path: str,
) -> ExactRepositoryFile:
    """Read a regular file from an exact commit without consulting the worktree."""

    return _RepositoryReader(repository_root).file(source_commit, source_path)


def repository_commit_is_ancestor(
    repository_root: str | Path,
    *,
    ancestor_commit: str,
    descendant_commit: str,
) -> bool:
    """Return Git ancestry for two exact full commits in the same repository."""

    return _RepositoryReader(repository_root).is_ancestor(ancestor_commit, descendant_commit)


def _find_symbol(module: ast.Module, qualified_symbol: str) -> ast.AST | None:
    body: list[ast.stmt] = module.body
    components = qualified_symbol.split(".")
    for index, component in enumerate(components):
        matches = [node for node in body if getattr(node, "name", None) == component]
        if len(matches) > 1:
            raise RetirementRepositoryError(f"repository symbol is ambiguous: {qualified_symbol}")
        if not matches:
            return None
        node = matches[0]
        if index < len(components) - 1:
            if not isinstance(node, ast.ClassDef):
                return None
            body = node.body
            continue
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return None
        return node
    return None


def _normalized_function_sha256(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Hash function logic while ignoring a consistent symbol rename."""

    original_name = node.name
    normalized = copy.deepcopy(node)
    normalized.name = "__retirement_surface__"
    for descendant in ast.walk(normalized):
        if isinstance(descendant, ast.Name) and descendant.id == original_name:
            descendant.id = "__retirement_surface__"
        elif isinstance(descendant, ast.Attribute) and descendant.attr == original_name:
            descendant.attr = "__retirement_surface__"
        elif isinstance(descendant, ast.Global | ast.Nonlocal):
            descendant.names = [
                "__retirement_surface__" if name == original_name else name for name in descendant.names
            ]
    encoded = ast.dump(normalized, annotate_fields=True, include_attributes=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptedRepositoryRetirementSurface:
    """A code-owned surface bound to an exact Git blob and normalized AST node."""

    descriptor: RepositoryRetirementSurface
    source_file: ExactRepositoryFile
    registry_file: ExactRepositoryFile
    registry_sha256: str
    source_node_kind: str
    source_node_sha256: str
    normalized_source_node_sha256: str
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        payload = self.payload()
        _digest(self.registry_sha256, label="registry_sha256")
        _digest(self.source_node_sha256, label="source_node_sha256")
        _digest(self.normalized_source_node_sha256, label="normalized_source_node_sha256")
        if (
            not _process_registry_is_current()
            or self.descriptor not in _REGISTERED_SURFACES
            or not accepted_repository_file_is_current(self.source_file)
            or not accepted_repository_file_is_current(self.registry_file)
            or self.source_file.source_path != self.descriptor.source_path
            or self.registry_file.source_path != _RETIREMENT_REGISTRY_PATH
            or self.registry_file.source_commit != self.source_file.source_commit
            or self.registry_file.source_tree_oid != self.source_file.source_tree_oid
            or not hmac.compare_digest(
                self.registry_sha256,
                SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            )
            or self.source_node_kind not in {"FunctionDef", "AsyncFunctionDef"}
            or self._process_authority is not _PROCESS_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _seal("repository-surface", payload),
            )
        ):
            raise RetirementRepositoryError("repository surface was not accepted by this process")

    def payload(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.payload(),
            "source_file": self.source_file.payload(),
            "registry": {
                "sha256": self.registry_sha256,
                "source_file": self.registry_file.payload(),
            },
            "source_node_kind": self.source_node_kind,
            "source_node_sha256": self.source_node_sha256,
            "normalized_source_node_sha256": self.normalized_source_node_sha256,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_repository_surface_is_current(value: object) -> bool:
    if (
        type(value) is not AcceptedRepositoryRetirementSurface
        or value._process_authority is not _PROCESS_AUTHORITY
        or not _process_registry_is_current()
        or not accepted_repository_file_is_current(value.source_file)
        or not accepted_repository_file_is_current(value.registry_file)
    ):
        return False
    expected = _seal("repository-surface", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


def _inspect_surface(
    reader: _RepositoryReader,
    *,
    source_commit: str,
    descriptor: RepositoryRetirementSurface,
) -> AcceptedRepositoryRetirementSurface:
    registry_file = reader.registry(source_commit)
    source_file = reader.file(source_commit, descriptor.source_path)
    node = _find_symbol(reader.module(source_file), descriptor.qualified_symbol)
    if node is None:
        raise RetirementRepositoryError(f"registered repository symbol is absent: {descriptor.candidate_id}")
    node_kind = type(node).__name__
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        raise RetirementRepositoryError("registered repository symbol is not a function")
    node_sha256 = hashlib.sha256(
        ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")
    ).hexdigest()
    normalized_node_sha256 = _normalized_function_sha256(node)
    payload: dict[str, object] = {
        "descriptor": descriptor.payload(),
        "source_file": source_file.payload(),
        "registry": {
            "sha256": SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            "source_file": registry_file.payload(),
        },
        "source_node_kind": node_kind,
        "source_node_sha256": node_sha256,
        "normalized_source_node_sha256": normalized_node_sha256,
    }
    return AcceptedRepositoryRetirementSurface(
        descriptor=descriptor,
        source_file=source_file,
        registry_file=registry_file,
        registry_sha256=SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
        source_node_kind=node_kind,
        source_node_sha256=node_sha256,
        normalized_source_node_sha256=normalized_node_sha256,
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_seal("repository-surface", payload),
    )


def inspect_repository_retirement_surface(
    repository_root: str | Path,
    *,
    source_commit: str,
    candidate_id: str,
) -> AcceptedRepositoryRetirementSurface:
    """Bind one code-owned surface to an exact full Git commit."""

    _require_process_registry()
    descriptor = _SURFACE_BY_ID.get(_safe_id(candidate_id, label="candidate_id"))
    if descriptor is None:
        raise RetirementRepositoryError("candidate_id is not in the code-owned inventory")
    return _inspect_surface(
        _RepositoryReader(repository_root),
        source_commit=source_commit,
        descriptor=descriptor,
    )


@dataclass(frozen=True, slots=True)
class RetirementRepositoryAssessment:
    """Read-only inventory result.  It cannot authorize source deletion."""

    source_commit: str
    source_tree_oid: str
    registry_file: ExactRepositoryFile
    registry_sha256: str
    surfaces: tuple[AcceptedRepositoryRetirementSurface, ...]
    eligible_candidate_ids: tuple[str, ...]
    protected_surface_ids: tuple[str, ...]
    reason: RetirementInventoryReason
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _git_oid(self.source_commit, label="source_commit")
        _git_oid(self.source_tree_oid, label="source_tree_oid")
        _digest(self.registry_sha256, label="registry_sha256")
        if (
            not _process_registry_is_current()
            or not accepted_repository_file_is_current(self.registry_file)
            or self.registry_file.source_path != _RETIREMENT_REGISTRY_PATH
            or self.registry_file.source_commit != self.source_commit
            or self.registry_file.source_tree_oid != self.source_tree_oid
            or not hmac.compare_digest(
                self.registry_sha256,
                SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            )
            or len(self.surfaces) != len(_REGISTERED_SURFACES)
            or not all(accepted_repository_surface_is_current(surface) for surface in self.surfaces)
        ):
            raise RetirementRepositoryError("assessment requires the complete accepted inventory")
        observed_ids = tuple(surface.descriptor.candidate_id for surface in self.surfaces)
        if observed_ids != tuple(surface.candidate_id for surface in _REGISTERED_SURFACES):
            raise RetirementRepositoryError("assessment surface order does not match the inventory")
        expected_eligible = tuple(
            item.candidate_id
            for item in _REGISTERED_SURFACES
            if item.surface_class is RetirementSurfaceClass.SEMANTIC_HEURISTIC
        )
        expected_protected = tuple(
            item.candidate_id
            for item in _REGISTERED_SURFACES
            if item.surface_class is not RetirementSurfaceClass.SEMANTIC_HEURISTIC
        )
        if (
            self.eligible_candidate_ids != expected_eligible
            or self.protected_surface_ids != expected_protected
        ):
            raise RetirementRepositoryError("assessment classification does not match the inventory")
        expected_reason = (
            RetirementInventoryReason.ELIGIBLE_CANDIDATE_PRESENT
            if expected_eligible
            else RetirementInventoryReason.NO_ELIGIBLE_CANDIDATE
        )
        if self.reason is not expected_reason:
            raise RetirementRepositoryError("assessment reason does not match the inventory")
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _seal("repository-assessment", self.payload()),
            )
        ):
            raise RetirementRepositoryError("repository assessment was not accepted by this process")

    @property
    def retirement_authorized(self) -> bool:
        return False

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_RETIREMENT_REPOSITORY_SCHEMA,
            "source_commit": self.source_commit,
            "source_tree_oid": self.source_tree_oid,
            "registry": {
                "sha256": self.registry_sha256,
                "source_file": self.registry_file.payload(),
            },
            "surfaces": [surface.payload() for surface in self.surfaces],
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "protected_surface_ids": list(self.protected_surface_ids),
            "reason": self.reason.value,
            "retirement_authorized": False,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_repository_assessment_is_current(value: object) -> bool:
    if (
        type(value) is not RetirementRepositoryAssessment
        or value._process_authority is not _PROCESS_AUTHORITY
        or not _process_registry_is_current()
        or not accepted_repository_file_is_current(value.registry_file)
        or not all(accepted_repository_surface_is_current(surface) for surface in value.surfaces)
    ):
        return False
    expected = _seal("repository-assessment", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


def assess_repository_retirement_inventory(
    repository_root: str | Path,
    *,
    source_commit: str,
) -> RetirementRepositoryAssessment:
    """Resolve the complete P6 inventory at one exact Git commit."""

    reader = _RepositoryReader(repository_root)
    commit, tree = reader.commit(source_commit)
    registry_file = reader.registry(commit)
    surfaces = tuple(
        _inspect_surface(reader, source_commit=commit, descriptor=descriptor)
        for descriptor in _REGISTERED_SURFACES
    )
    eligible = tuple(
        descriptor.candidate_id
        for descriptor in _REGISTERED_SURFACES
        if descriptor.surface_class is RetirementSurfaceClass.SEMANTIC_HEURISTIC
    )
    protected = tuple(
        descriptor.candidate_id
        for descriptor in _REGISTERED_SURFACES
        if descriptor.surface_class is not RetirementSurfaceClass.SEMANTIC_HEURISTIC
    )
    reason = (
        RetirementInventoryReason.ELIGIBLE_CANDIDATE_PRESENT
        if eligible
        else RetirementInventoryReason.NO_ELIGIBLE_CANDIDATE
    )
    payload: dict[str, object] = {
        "schema": SUPERVISOR_RETIREMENT_REPOSITORY_SCHEMA,
        "source_commit": commit,
        "source_tree_oid": tree,
        "registry": {
            "sha256": SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            "source_file": registry_file.payload(),
        },
        "surfaces": [surface.payload() for surface in surfaces],
        "eligible_candidate_ids": list(eligible),
        "protected_surface_ids": list(protected),
        "reason": reason.value,
        "retirement_authorized": False,
    }
    return RetirementRepositoryAssessment(
        source_commit=commit,
        source_tree_oid=tree,
        registry_file=registry_file,
        registry_sha256=SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
        surfaces=surfaces,
        eligible_candidate_ids=eligible,
        protected_surface_ids=protected,
        reason=reason,
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_seal("repository-assessment", payload),
    )


@dataclass(frozen=True, slots=True)
class _DeletionScanReceipt:
    file_count: int
    byte_count: int
    ast_node_count: int
    files_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "scope": _DELETION_SCAN_SCOPE,
            "file_count": self.file_count,
            "byte_count": self.byte_count,
            "ast_node_count": self.ast_node_count,
            "files_sha256": self.files_sha256,
            "source_paths_included": False,
            "source_bodies_included": False,
        }


def _deletion_scope_entries(
    reader: _RepositoryReader,
    *,
    source_commit: str,
) -> tuple[tuple[str, str, int], ...]:
    commit, _ = reader.commit(source_commit)
    listing = _run_git(
        reader.root,
        "ls-tree",
        "-r",
        "-l",
        "-z",
        "--full-tree",
        commit,
        "--",
        _DELETION_SCAN_PREFIX.rstrip("/"),
    ).stdout
    entries: list[tuple[str, str, int]] = []
    observed_paths: set[str] = set()
    observed_bytes = 0
    for raw_entry in listing.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            identity, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, blob_oid, raw_size = identity.decode("ascii", errors="strict").split()
            source_path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise RetirementRepositoryError("Git returned an ambiguous deletion-scan entry") from exc
        source_path = _repository_path(source_path, label="deletion scan path")
        if not (
            source_path.startswith(_DELETION_SCAN_PREFIX) and source_path.endswith(_DELETION_SCAN_SUFFIX)
        ):
            continue
        if source_path in observed_paths:
            raise RetirementRepositoryError("deletion scan contains a duplicate path")
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise RetirementRepositoryError("deletion scan Python source is not a regular blob")
        _git_oid(blob_oid, label="deletion scan blob_oid")
        if not raw_size.isdecimal():
            raise RetirementRepositoryError("Git returned an invalid deletion-scan blob size")
        blob_size = int(raw_size)
        if blob_size > _MAX_GIT_OUTPUT_BYTES:
            raise RetirementRepositoryError("deletion scan source exceeds its per-file byte budget")
        observed_paths.add(source_path)
        if len(observed_paths) > _MAX_DELETION_SCAN_FILES:
            raise RetirementRepositoryError("deletion scan exceeded its file budget")
        observed_bytes += blob_size
        if observed_bytes > _MAX_DELETION_SCAN_BYTES:
            raise RetirementRepositoryError("deletion scan exceeded its byte budget")
        entries.append((source_path, blob_oid, blob_size))
    if not entries:
        raise RetirementRepositoryError("deletion scan found no Python source")
    return tuple(entries)


def _require_normalized_predecessor_absent(
    reader: _RepositoryReader,
    *,
    deletion_commit: str,
    normalized_predecessor_sha256: str,
) -> _DeletionScanReceipt:
    _digest(normalized_predecessor_sha256, label="normalized_predecessor_sha256")
    entries = _deletion_scope_entries(reader, source_commit=deletion_commit)
    byte_count = 0
    ast_node_count = 0
    identities: list[dict[str, object]] = []
    for source_path, blob_oid, blob_size in entries:
        source_file = reader.uncached_file(deletion_commit, source_path)
        if source_file.blob_oid != blob_oid or source_file.byte_count != blob_size:
            raise RetirementRepositoryError("deletion scan tree identity changed during inspection")
        byte_count += source_file.byte_count
        if byte_count > _MAX_DELETION_SCAN_BYTES:
            raise RetirementRepositoryError("deletion scan exceeded its byte budget")
        module = reader.uncached_module(source_file)
        for node in ast.walk(module):
            ast_node_count += 1
            if ast_node_count > _MAX_DELETION_SCAN_AST_NODES:
                raise RetirementRepositoryError("deletion scan exceeded its AST-node budget")
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and hmac.compare_digest(
                _normalized_function_sha256(node),
                normalized_predecessor_sha256,
            ):
                raise RetirementRepositoryError("normalized predecessor AST remains in friday/**/*.py")
        identities.append(
            {
                "source_path": source_file.source_path,
                "blob_oid": source_file.blob_oid,
                "file_sha256": source_file.file_sha256,
                "byte_count": source_file.byte_count,
            }
        )
    return _DeletionScanReceipt(
        file_count=len(entries),
        byte_count=byte_count,
        ast_node_count=ast_node_count,
        files_sha256=canonical_sha256(identities),
    )


@dataclass(frozen=True, slots=True)
class AcceptedRepositoryRetirementCandidate:
    """Exact, process-sealed source deletion proposed for later release review."""

    predecessor_surface: AcceptedRepositoryRetirementSurface
    deletion_file: ExactRepositoryFile
    deletion_registry_file: ExactRepositoryFile
    registry_sha256: str
    deletion_scan_file_count: int
    deletion_scan_byte_count: int
    deletion_scan_ast_node_count: int
    deletion_scan_files_sha256: str
    previous_documentation_sha256: str
    documentation_file: ExactRepositoryFile
    previous_status_registry_sha256: str
    status_registry_file: ExactRepositoryFile
    replacement_policy_file: ExactRepositoryFile
    replacement_manifest_file: ExactRepositoryFile
    replacement_adapter_registry_file: ExactRepositoryFile
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        files = (
            self.deletion_file,
            self.deletion_registry_file,
            self.documentation_file,
            self.status_registry_file,
            self.replacement_policy_file,
            self.replacement_manifest_file,
            self.replacement_adapter_registry_file,
        )
        payload = self.payload()
        _digest(self.registry_sha256, label="registry_sha256")
        _digest(self.deletion_scan_files_sha256, label="deletion_scan_files_sha256")
        if (
            not _process_registry_is_current()
            or not accepted_repository_surface_is_current(self.predecessor_surface)
            or self.predecessor_surface.descriptor.surface_class
            is not RetirementSurfaceClass.SEMANTIC_HEURISTIC
            or not all(accepted_repository_file_is_current(item) for item in files)
            or len({item.source_commit for item in files}) != 1
            or len({item.source_tree_oid for item in files}) != 1
            or self.deletion_file.source_path != self.predecessor_surface.descriptor.source_path
            or self.deletion_registry_file.source_path != _RETIREMENT_REGISTRY_PATH
            or not hmac.compare_digest(
                self.registry_sha256,
                SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            )
            or not 1 <= self.deletion_scan_file_count <= _MAX_DELETION_SCAN_FILES
            or not 0 <= self.deletion_scan_byte_count <= _MAX_DELETION_SCAN_BYTES
            or not 0 <= self.deletion_scan_ast_node_count <= _MAX_DELETION_SCAN_AST_NODES
            or self.documentation_file.source_path != _DOCUMENTATION_PATH
            or self.status_registry_file.source_path != _STATUS_REGISTRY_PATH
            or self.replacement_policy_file.source_path != _REPLACEMENT_POLICY_PATH
            or self.replacement_manifest_file.source_path != _REPLACEMENT_MANIFEST_PATH
            or self.replacement_adapter_registry_file.source_path != _REPLACEMENT_ADAPTER_REGISTRY_PATH
            or self.previous_documentation_sha256 == self.documentation_file.file_sha256
            or self.previous_status_registry_sha256 == self.status_registry_file.file_sha256
            or self._process_authority is not _PROCESS_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _seal("repository-candidate", payload),
            )
        ):
            raise RetirementRepositoryError("retirement candidate was not accepted by this process")
        _digest(self.previous_documentation_sha256, label="previous_documentation_sha256")
        _digest(self.previous_status_registry_sha256, label="previous_status_registry_sha256")

    @property
    def candidate_id(self) -> str:
        return self.predecessor_surface.descriptor.candidate_id

    @property
    def journey(self) -> TaskClass:
        return self.predecessor_surface.descriptor.journey

    @property
    def deletion_commit(self) -> str:
        return self.deletion_file.source_commit

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_RETIREMENT_CANDIDATE_SCHEMA,
            "predecessor_surface": self.predecessor_surface.payload(),
            "deletion_file": self.deletion_file.payload(),
            "registry": {
                "sha256": self.registry_sha256,
                "deletion_file": self.deletion_registry_file.payload(),
            },
            "qualified_symbol_absent": True,
            "normalized_predecessor_absent": True,
            "deletion_scan": {
                **_DeletionScanReceipt(
                    file_count=self.deletion_scan_file_count,
                    byte_count=self.deletion_scan_byte_count,
                    ast_node_count=self.deletion_scan_ast_node_count,
                    files_sha256=self.deletion_scan_files_sha256,
                ).payload(),
                "source_tree_oid": self.deletion_file.source_tree_oid,
                "normalized_predecessor_sha256": (self.predecessor_surface.normalized_source_node_sha256),
                "matching_function_count": 0,
            },
            "documentation": {
                "previous_file_sha256": self.previous_documentation_sha256,
                "current_file": self.documentation_file.payload(),
            },
            "status_registry": {
                "previous_file_sha256": self.previous_status_registry_sha256,
                "current_file": self.status_registry_file.payload(),
            },
            "replacement_sources": {
                "policy": self.replacement_policy_file.payload(),
                "manifest": self.replacement_manifest_file.payload(),
                "adapter_registry": self.replacement_adapter_registry_file.payload(),
            },
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_repository_candidate_is_current(value: object) -> bool:
    if (
        type(value) is not AcceptedRepositoryRetirementCandidate
        or value._process_authority is not _PROCESS_AUTHORITY
        or not accepted_repository_surface_is_current(value.predecessor_surface)
    ):
        return False
    files = (
        value.deletion_file,
        value.deletion_registry_file,
        value.documentation_file,
        value.status_registry_file,
        value.replacement_policy_file,
        value.replacement_manifest_file,
        value.replacement_adapter_registry_file,
    )
    if not all(accepted_repository_file_is_current(item) for item in files):
        return False
    expected = _seal("repository-candidate", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


def accept_repository_retirement_candidate(
    repository_root: str | Path,
    *,
    candidate_id: str,
    predecessor_commit: str,
    deletion_commit: str,
) -> AcceptedRepositoryRetirementCandidate:
    """Accept an exact deletion only for a code-reviewed semantic surface.

    The current inventory intentionally has no such surface, so current HEAD
    yields ``NO_ELIGIBLE_CANDIDATE`` and every direct nomination fails closed.
    """

    _require_process_registry()
    descriptor = _SURFACE_BY_ID.get(_safe_id(candidate_id, label="candidate_id"))
    if descriptor is None:
        raise RetirementRepositoryError("candidate_id is not in the code-owned inventory")
    if descriptor.surface_class is not RetirementSurfaceClass.SEMANTIC_HEURISTIC:
        raise RetirementRepositoryError("surface_is_not_semantic")
    if predecessor_commit == deletion_commit:
        raise RetirementRepositoryError("deletion_commit must differ from predecessor_commit")
    reader = _RepositoryReader(repository_root)
    if not reader.is_ancestor(predecessor_commit, deletion_commit):
        raise RetirementRepositoryError("predecessor_commit must be an ancestor of deletion_commit")
    predecessor = _inspect_surface(
        reader,
        source_commit=predecessor_commit,
        descriptor=descriptor,
    )
    deletion_registry_file = reader.registry(deletion_commit)
    deletion_file = reader.file(deletion_commit, descriptor.source_path)
    if _find_symbol(reader.module(deletion_file), descriptor.qualified_symbol) is not None:
        raise RetirementRepositoryError("retirement candidate did not delete the exact symbol")
    if hmac.compare_digest(deletion_file.file_sha256, predecessor.source_file.file_sha256):
        raise RetirementRepositoryError("retirement candidate source file did not change")
    deletion_scan = _require_normalized_predecessor_absent(
        reader,
        deletion_commit=deletion_commit,
        normalized_predecessor_sha256=predecessor.normalized_source_node_sha256,
    )

    previous_documentation = reader.file(predecessor_commit, _DOCUMENTATION_PATH)
    documentation = reader.file(deletion_commit, _DOCUMENTATION_PATH)
    previous_status = reader.file(predecessor_commit, _STATUS_REGISTRY_PATH)
    status = reader.file(deletion_commit, _STATUS_REGISTRY_PATH)
    if hmac.compare_digest(previous_documentation.file_sha256, documentation.file_sha256):
        raise RetirementRepositoryError("retirement documentation was not updated")
    if hmac.compare_digest(previous_status.file_sha256, status.file_sha256):
        raise RetirementRepositoryError("retirement status registry was not updated")

    policy = reader.file(deletion_commit, _REPLACEMENT_POLICY_PATH)
    manifest = reader.file(deletion_commit, _REPLACEMENT_MANIFEST_PATH)
    adapter_registry = reader.file(deletion_commit, _REPLACEMENT_ADAPTER_REGISTRY_PATH)
    payload: dict[str, object] = {
        "schema": SUPERVISOR_RETIREMENT_CANDIDATE_SCHEMA,
        "predecessor_surface": predecessor.payload(),
        "deletion_file": deletion_file.payload(),
        "registry": {
            "sha256": SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
            "deletion_file": deletion_registry_file.payload(),
        },
        "qualified_symbol_absent": True,
        "normalized_predecessor_absent": True,
        "deletion_scan": {
            **deletion_scan.payload(),
            "source_tree_oid": deletion_file.source_tree_oid,
            "normalized_predecessor_sha256": predecessor.normalized_source_node_sha256,
            "matching_function_count": 0,
        },
        "documentation": {
            "previous_file_sha256": previous_documentation.file_sha256,
            "current_file": documentation.payload(),
        },
        "status_registry": {
            "previous_file_sha256": previous_status.file_sha256,
            "current_file": status.payload(),
        },
        "replacement_sources": {
            "policy": policy.payload(),
            "manifest": manifest.payload(),
            "adapter_registry": adapter_registry.payload(),
        },
    }
    return AcceptedRepositoryRetirementCandidate(
        predecessor_surface=predecessor,
        deletion_file=deletion_file,
        deletion_registry_file=deletion_registry_file,
        registry_sha256=SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
        deletion_scan_file_count=deletion_scan.file_count,
        deletion_scan_byte_count=deletion_scan.byte_count,
        deletion_scan_ast_node_count=deletion_scan.ast_node_count,
        deletion_scan_files_sha256=deletion_scan.files_sha256,
        previous_documentation_sha256=previous_documentation.file_sha256,
        documentation_file=documentation,
        previous_status_registry_sha256=previous_status.file_sha256,
        status_registry_file=status,
        replacement_policy_file=policy,
        replacement_manifest_file=manifest,
        replacement_adapter_registry_file=adapter_registry,
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_seal("repository-candidate", payload),
    )


__all__ = [
    "AcceptedRepositoryRetirementCandidate",
    "AcceptedRepositoryRetirementSurface",
    "ExactRepositoryFile",
    "RepositoryRetirementSurface",
    "RetirementInventoryReason",
    "RetirementRepositoryAssessment",
    "RetirementRepositoryError",
    "RetirementSurfaceClass",
    "SUPERVISOR_RETIREMENT_CANDIDATE_SCHEMA",
    "SUPERVISOR_RETIREMENT_REGISTRY_SCHEMA",
    "SUPERVISOR_RETIREMENT_REGISTRY_SHA256",
    "SUPERVISOR_RETIREMENT_REPOSITORY_SCHEMA",
    "accept_repository_retirement_candidate",
    "accepted_repository_assessment_is_current",
    "accepted_repository_candidate_is_current",
    "accepted_repository_file_is_current",
    "accepted_repository_surface_is_current",
    "assess_repository_retirement_inventory",
    "inspect_repository_retirement_surface",
    "read_exact_repository_file",
    "registered_retirement_surfaces",
    "repository_commit_is_ancestor",
]
