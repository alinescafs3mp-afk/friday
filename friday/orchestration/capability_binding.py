"""Process-private supervisor bindings to real permission and adapter registries.

The public capability manifest intentionally contains symbolic supervisor IDs
only.  This module resolves those symbols to the concrete security, tool and
adapter identities that code can execute, and keeps the exact resolution on the
private side of Policy Kernel admission.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any

from friday.orchestration.supervisor_contracts import (
    ARCHIVE_SEARCH_ID,
    CONVERSATION_WINDOW_READ_ID,
    FILE_CURRENT_READ_ID,
    WEB_SEARCH_CURRENT_ID,
    CapabilityEffectClass,
    CapabilityManifest,
    SupervisorContractError,
    canonical_sha256,
)
from friday.permissions import AuthorizationService, CapabilityDefinition

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _qualified_name(value: object) -> str:
    module = str(getattr(value, "__module__", "") or "")
    name = str(getattr(value, "__qualname__", "") or "")
    return f"{module}.{name}" if module and name else ""


def _permission_payload(definition: CapabilityDefinition) -> dict[str, Any]:
    return {
        "security_id": definition.security_id,
        "description": definition.description,
        "category": definition.category,
        "risk_level": definition.risk_level,
        "default_presets": list(definition.default_presets),
        "default_requires_hitl": definition.default_requires_hitl,
        "source": definition.source,
    }


@dataclass(frozen=True, slots=True)
class OperationalCapabilityBinding:
    """One exact private resolution of a symbolic supervisor capability."""

    supervisor_capability_id: str
    effect_class: CapabilityEffectClass
    security_id: str
    tool_id: str
    adapter_id: str
    permission_identity_sha256: str
    adapter_identity_sha256: str
    permission_registered: bool
    adapter_registered: bool

    def __post_init__(self) -> None:
        if not self.supervisor_capability_id:
            raise SupervisorContractError("operational binding needs a supervisor capability id")
        for label, value in (
            ("permission_identity_sha256", self.permission_identity_sha256),
            ("adapter_identity_sha256", self.adapter_identity_sha256),
        ):
            if _DIGEST.fullmatch(value) is None:
                raise SupervisorContractError(f"{label} must be a lowercase SHA-256 digest")
        if self.available and not all((self.security_id, self.tool_id, self.adapter_id)):
            raise SupervisorContractError("available operational binding needs resolved identities")

    @property
    def available(self) -> bool:
        return self.permission_registered and self.adapter_registered

    def identity_payload(self) -> dict[str, Any]:
        return {
            "supervisor_capability_id": self.supervisor_capability_id,
            "effect_class": self.effect_class.value,
            "security_id": self.security_id,
            "tool_id": self.tool_id,
            "adapter_id": self.adapter_id,
            "permission_identity_sha256": self.permission_identity_sha256,
            "adapter_identity_sha256": self.adapter_identity_sha256,
            "permission_registered": self.permission_registered,
            "adapter_registered": self.adapter_registered,
        }


@dataclass(frozen=True, slots=True)
class CapabilityBindingSnapshot:
    """Immutable code-owned registry view captured for one admission attempt."""

    bindings: tuple[OperationalCapabilityBinding, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.supervisor_capability_id for item in self.bindings)
        if not ids or len(ids) != len(set(ids)):
            raise SupervisorContractError("operational capability binding ids must be unique")

    def binding_for(self, capability_id: str) -> OperationalCapabilityBinding | None:
        return next(
            (item for item in self.bindings if item.supervisor_capability_id == capability_id),
            None,
        )

    def digest_hex(self) -> str:
        return canonical_sha256(
            {
                "schema": "friday.supervisor-capability-binding.private.v1",
                "bindings": [item.identity_payload() for item in self.bindings],
            }
        )


@dataclass(frozen=True, slots=True)
class _BoundCapabilityManifest(CapabilityManifest):
    """A public manifest carrying a non-serialized private registry witness."""

    _binding_snapshot_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if _DIGEST.fullmatch(self._binding_snapshot_sha256) is None:
            raise SupervisorContractError("manifest binding witness must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class _AdapterResolution:
    security_id: str
    tool_id: str
    adapter_id: str
    identity_sha256: str
    registered: bool


def _missing_adapter(*, security_id: str = "", tool_id: str = "", adapter_id: str = "") -> _AdapterResolution:
    payload = {
        "security_id": security_id,
        "tool_id": tool_id,
        "adapter_id": adapter_id,
        "registered": False,
    }
    return _AdapterResolution(
        security_id=security_id,
        tool_id=tool_id,
        adapter_id=adapter_id,
        identity_sha256=canonical_sha256(payload),
        registered=False,
    )


def _file_adapter_resolution() -> _AdapterResolution:
    expected_adapter = "friday.orchestration.file_read.V12FileReadHandler"
    try:
        from friday.file_evidence_reader import prepare_current_turn_file_evidence
        from friday.orchestration.contracts import RouteClass, ToolEffect
        from friday.orchestration.file_read import V12FileReadHandler
    except Exception:
        return _missing_adapter(
            security_id="files.read",
            tool_id="file_read",
            adapter_id=expected_adapter,
        )

    adapter_id = _qualified_name(V12FileReadHandler)
    reader_id = _qualified_name(prepare_current_turn_file_evidence)
    tool_id = str(getattr(V12FileReadHandler, "route", "") or "")
    registered = bool(
        adapter_id == expected_adapter
        and tool_id == RouteClass.FILE_READ.value
        and getattr(V12FileReadHandler, "effect", None) is ToolEffect.READ
        and callable(getattr(V12FileReadHandler, "prepare", None))
        and reader_id == "friday.file_evidence_reader.prepare_current_turn_file_evidence"
    )
    payload = {
        "security_id": "files.read",
        "tool_id": tool_id,
        "adapter_id": adapter_id,
        "reader_id": reader_id,
        "effect": str(getattr(V12FileReadHandler, "effect", "") or ""),
        "registered": registered,
    }
    return _AdapterResolution(
        security_id="files.read",
        tool_id=tool_id,
        adapter_id=adapter_id,
        identity_sha256=canonical_sha256(payload),
        registered=registered,
    )


def _archive_adapter_resolution(authorization: AuthorizationService) -> _AdapterResolution:
    expected_adapter = "friday.execution_kernel.ExecutionKernel._archive_search"
    try:
        from friday.execution_kernel import ExecutionKernel

        kernel = ExecutionKernel(authorization)
        tool = kernel.get_tool("archive_search")
        adapter_id = _qualified_name(ExecutionKernel._archive_search)
    except Exception:
        return _missing_adapter(
            security_id="search.use",
            tool_id="archive_search",
            adapter_id=expected_adapter,
        )

    security_id = str(getattr(tool, "security_id", "") or "")
    tool_id = str(getattr(tool, "name", "") or "")
    registered = bool(
        tool is not None
        and security_id == "search.use"
        and tool_id == "archive_search"
        and str(getattr(tool, "risk", "") or "") == "observe"
        and adapter_id == expected_adapter
        and callable(getattr(kernel, "_archive_search", None))
    )
    payload = {
        "security_id": security_id,
        "tool_id": tool_id,
        "adapter_id": adapter_id,
        "risk": str(getattr(tool, "risk", "") or ""),
        "execution_scopes": sorted(getattr(tool, "allowed_execution_scopes", ())),
        "input_schema_sha256": canonical_sha256(getattr(tool, "parameters", {})),
        "registered": registered,
    }
    return _AdapterResolution(
        security_id=security_id,
        tool_id=tool_id,
        adapter_id=adapter_id,
        identity_sha256=canonical_sha256(payload),
        registered=registered,
    )


def _transient_web_adapter_resolution() -> _AdapterResolution:
    try:
        module = importlib.import_module("friday.orchestration.transient_web_comparison")
        security_id = str(module.TRANSIENT_WEB_SECURITY_ID)
        adapter_id = str(module.TRANSIENT_WEB_ADAPTER_ID)
        adapter = module.TransientWebComparisonAdapter
        sealer = module.seal_explicit_public_web_query
    except Exception:
        return _missing_adapter()

    tool_id = f"{_qualified_name(adapter)}.research"
    registered = bool(
        security_id
        and adapter_id
        and _qualified_name(adapter)
        == "friday.orchestration.transient_web_comparison.TransientWebComparisonAdapter"
        and callable(getattr(adapter, "research", None))
        and callable(sealer)
    )
    payload = {
        "security_id": security_id,
        "tool_id": tool_id,
        "adapter_id": adapter_id,
        "implementation_id": _qualified_name(adapter),
        "sealer_id": _qualified_name(sealer),
        "registered": registered,
    }
    return _AdapterResolution(
        security_id=security_id,
        tool_id=tool_id,
        adapter_id=adapter_id,
        identity_sha256=canonical_sha256(payload),
        registered=registered,
    )


def _permission_identity(
    definitions: dict[str, CapabilityDefinition],
    security_id: str,
) -> tuple[str, bool]:
    definition = definitions.get(security_id)
    if definition is None:
        return canonical_sha256({"security_id": security_id, "registered": False}), False
    return canonical_sha256(_permission_payload(definition)), True


def _binding(
    capability_id: str,
    resolution: _AdapterResolution,
    definitions: dict[str, CapabilityDefinition],
) -> OperationalCapabilityBinding:
    permission_digest, permission_registered = _permission_identity(
        definitions,
        resolution.security_id,
    )
    return OperationalCapabilityBinding(
        supervisor_capability_id=capability_id,
        effect_class=CapabilityEffectClass.READ,
        security_id=resolution.security_id,
        tool_id=resolution.tool_id,
        adapter_id=resolution.adapter_id,
        permission_identity_sha256=permission_digest,
        adapter_identity_sha256=resolution.identity_sha256,
        permission_registered=permission_registered,
        adapter_registered=resolution.registered,
    )


def operational_capability_snapshot() -> CapabilityBindingSnapshot:
    """Resolve the current concrete permission and adapter registry fail-closed."""

    authorization = AuthorizationService()
    definitions = {item.security_id: item for item in authorization.list_capabilities()}
    file_resolution = _file_adapter_resolution()
    archive_resolution = _archive_adapter_resolution(authorization)
    web_resolution = _transient_web_adapter_resolution()
    return CapabilityBindingSnapshot(
        bindings=(
            _binding(FILE_CURRENT_READ_ID, file_resolution, definitions),
            _binding(ARCHIVE_SEARCH_ID, archive_resolution, definitions),
            _binding(WEB_SEARCH_CURRENT_ID, web_resolution, definitions),
            _binding(
                CONVERSATION_WINDOW_READ_ID,
                _missing_adapter(security_id="conversations.read"),
                definitions,
            ),
        )
    )


def bind_manifest_to_snapshot(
    manifest: CapabilityManifest,
    snapshot: CapabilityBindingSnapshot,
) -> CapabilityManifest:
    """Attach an exact private registry witness without widening public JSON."""

    return _BoundCapabilityManifest(
        capabilities=manifest.capabilities,
        model_roles=manifest.model_roles,
        manifest_id=manifest.manifest_id,
        _binding_snapshot_sha256=snapshot.digest_hex(),
    )


def manifest_binding_snapshot_sha256(manifest: CapabilityManifest) -> str | None:
    """Return the private witness only for an in-process code-built manifest."""

    if not isinstance(manifest, _BoundCapabilityManifest):
        return None
    witness = manifest._binding_snapshot_sha256
    return witness if _DIGEST.fullmatch(witness) is not None else None


def manifest_matches_snapshot(
    manifest: CapabilityManifest,
    snapshot: CapabilityBindingSnapshot,
) -> bool:
    """Prove that public availability/effects match the private resolution."""

    if manifest_binding_snapshot_sha256(manifest) != snapshot.digest_hex():
        return False
    for descriptor in manifest.capabilities:
        binding = snapshot.binding_for(descriptor.id)
        if binding is None or binding.effect_class is not descriptor.effect_class:
            return False
        public_available = descriptor.availability.value in {"available", "partial"}
        if public_available != binding.available:
            return False
    return True
