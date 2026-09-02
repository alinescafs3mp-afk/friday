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

_OBSIDIAN_EFFECT_SECURITY_ID = "obsidian.write"
_OBSIDIAN_EFFECT_TOOL_RISK = "mutate"
_OBSIDIAN_EFFECT_CONTOURS = (
    ("obsidian_note_mutation:create", "create", "obsidian_create_note"),
    ("obsidian_note_mutation:append", "append", "obsidian_append_note"),
)


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


def _message_exact_adapter_resolution(
    definitions: dict[str, CapabilityDefinition],
) -> _AdapterResolution:
    """Resolve the code-owned queryless conversation lane and both permissions."""

    expected_adapter = "friday.retrieval.message_exact_internal.MessageExactInternalAdapter"
    try:
        from friday.retrieval.message_exact_internal import (
            MESSAGE_EXACT_ADAPTER_BINDING,
            MESSAGE_EXACT_INTERNAL_ADAPTER_ID,
            MESSAGE_EXACT_SECURITY_IDS,
            MessageExactInternalAdapter,
        )
    except Exception:
        return _missing_adapter(
            security_id="conversations.read",
            tool_id=CONVERSATION_WINDOW_READ_ID,
            adapter_id=expected_adapter,
        )

    security_ids = tuple(MESSAGE_EXACT_SECURITY_IDS)
    try:
        adapter_binding_sha256 = MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256()
    except Exception:
        return _missing_adapter(
            security_id="conversations.read",
            tool_id=CONVERSATION_WINDOW_READ_ID,
            adapter_id=expected_adapter,
        )
    permission_witnesses: list[dict[str, Any]] = []
    permissions_registered = True
    for security_id in security_ids:
        definition = definitions.get(security_id)
        registered = definition is not None
        permissions_registered = permissions_registered and registered
        permission_witnesses.append(
            {
                "security_id": security_id,
                "registered": registered,
                "identity_sha256": canonical_sha256(
                    _permission_payload(definition)
                    if definition is not None
                    else {"security_id": security_id, "registered": False}
                ),
            }
        )
    adapter_id = str(MESSAGE_EXACT_INTERNAL_ADAPTER_ID)
    tool_id = str(MESSAGE_EXACT_ADAPTER_BINDING.capability_id)
    method_ids = {
        name: _qualified_name(getattr(MessageExactInternalAdapter, name, None))
        for name in (
            "prepare_in_transaction",
            "project_for_model",
            "reauthorize_for_publication_in_transaction",
        )
    }
    registered = bool(
        adapter_id == expected_adapter
        and _qualified_name(MessageExactInternalAdapter) == expected_adapter
        and MESSAGE_EXACT_ADAPTER_BINDING.adapter_id == expected_adapter
        and tool_id == CONVERSATION_WINDOW_READ_ID
        and MESSAGE_EXACT_ADAPTER_BINDING.security_ids
        == (
            "conversations.read",
            "search.use",
        )
        and security_ids == MESSAGE_EXACT_ADAPTER_BINDING.security_ids
        and MESSAGE_EXACT_ADAPTER_BINDING.effect_class == "read"
        and MESSAGE_EXACT_ADAPTER_BINDING.model_visible is False
        and method_ids
        == {
            "prepare_in_transaction": f"{expected_adapter}.prepare_in_transaction",
            "project_for_model": f"{expected_adapter}.project_for_model",
            "reauthorize_for_publication_in_transaction": (
                f"{expected_adapter}.reauthorize_for_publication_in_transaction"
            ),
        }
        and permissions_registered
    )
    payload = {
        "security_id": security_ids[0] if security_ids else "",
        "security_ids": list(security_ids),
        "permission_witnesses": permission_witnesses,
        "tool_id": tool_id,
        "adapter_id": adapter_id,
        "adapter_binding_sha256": adapter_binding_sha256,
        "implementation_id": _qualified_name(MessageExactInternalAdapter),
        "method_ids": method_ids,
        "effect_class": MESSAGE_EXACT_ADAPTER_BINDING.effect_class,
        "model_visible": MESSAGE_EXACT_ADAPTER_BINDING.model_visible,
        "registered": registered,
    }
    return _AdapterResolution(
        security_id=security_ids[0] if security_ids else "",
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
    message_resolution = _message_exact_adapter_resolution(definitions)
    return CapabilityBindingSnapshot(
        bindings=(
            _binding(FILE_CURRENT_READ_ID, file_resolution, definitions),
            _binding(ARCHIVE_SEARCH_ID, archive_resolution, definitions),
            _binding(WEB_SEARCH_CURRENT_ID, web_resolution, definitions),
            _binding(CONVERSATION_WINDOW_READ_ID, message_resolution, definitions),
        )
    )


def _handler_binds_runtime(handler: object, runtime: object) -> bool:
    """Prove that one code-owned closure targets this exact runtime instance."""

    code = getattr(handler, "__code__", None)
    closure = getattr(handler, "__closure__", None)
    if code is None or not isinstance(closure, tuple) or len(code.co_freevars) != len(closure):
        return False
    for name, cell in zip(code.co_freevars, closure, strict=True):
        if name != "runtime":
            continue
        try:
            return cell.cell_contents is runtime
        except ValueError:
            return False
    return False


def _effect_tool_identity_payload(
    tool: object,
    *,
    action: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    handler = getattr(tool, "handler", None)
    approval_predicate = getattr(tool, "approval_predicate", None)
    return {
        "schema": "friday.supervisor-effect-operational-contour-binding.v1",
        "body_free": True,
        "capability": "obsidian_note_mutation",
        "action": action,
        "symbol_manifest_sha256": manifest_sha256,
        "kernel_id": "friday.execution_kernel.ExecutionKernel",
        "authorization_id": "friday.permissions.AuthorizationService",
        "runtime_id": "friday.organs.obsidian.runtime.ObsidianRuntime",
        "tool_id": str(getattr(tool, "name", "") or ""),
        "security_id": str(getattr(tool, "security_id", "") or ""),
        "effect_class": CapabilityEffectClass.WRITE.value,
        "risk": str(getattr(tool, "risk", "") or ""),
        "description_sha256": canonical_sha256(str(getattr(tool, "description", "") or "")),
        "input_schema_sha256": canonical_sha256(getattr(tool, "parameters", {})),
        "handler_id": _qualified_name(handler),
        "allowed_execution_scopes": sorted(getattr(tool, "allowed_execution_scopes", ())),
        "timeout_sec": getattr(tool, "timeout_sec", None),
        "model_visible": getattr(tool, "model_visible", None),
        "approval_predicate_id": _qualified_name(approval_predicate),
        "enabled": True,
        "permission_registered": True,
        "kernel_registered": True,
        "runtime_bound": True,
    }


def expected_effect_capability_snapshot() -> CapabilityBindingSnapshot:
    """Derive the stable code-owned P5 contour identity without activation."""

    from friday.execution_kernel import ToolSpec
    from friday.orchestration.supervisor_effect_intent import (
        SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
    )
    from friday.organs import ServiceContext
    from friday.organs.obsidian import OBSIDIAN_WRITE
    from friday.organs.obsidian.runtime import ObsidianRuntime
    from friday.organs.obsidian.tools import build_obsidian_tools

    sentinel_runtime = object.__new__(ObsidianRuntime)
    tools = {
        tool.name: tool
        for tool in build_obsidian_tools(
            ServiceContext(
                settings=object(),  # type: ignore[arg-type]
                storage=object(),
                kg=object(),
                ingestion=object(),
                obsidian=sentinel_runtime,
            )
        )
        if tool.name in {item[2] for item in _OBSIDIAN_EFFECT_CONTOURS}
    }
    permission_digest = canonical_sha256(_permission_payload(OBSIDIAN_WRITE))
    bindings: list[OperationalCapabilityBinding] = []
    for capability_id, action, tool_name in _OBSIDIAN_EFFECT_CONTOURS:
        tool = tools.get(tool_name)
        if (
            type(tool) is not ToolSpec
            or tool.security_id != _OBSIDIAN_EFFECT_SECURITY_ID
            or tool.risk != _OBSIDIAN_EFFECT_TOOL_RISK
            or tool.model_visible is not True
            or not callable(tool.handler)
            or not _handler_binds_runtime(tool.handler, sentinel_runtime)
        ):
            raise SupervisorContractError("expected Obsidian effect contour is unavailable")
        bindings.append(
            OperationalCapabilityBinding(
                supervisor_capability_id=capability_id,
                effect_class=CapabilityEffectClass.WRITE,
                security_id=_OBSIDIAN_EFFECT_SECURITY_ID,
                tool_id=tool_name,
                adapter_id=_qualified_name(tool.handler),
                permission_identity_sha256=permission_digest,
                adapter_identity_sha256=canonical_sha256(
                    _effect_tool_identity_payload(
                        tool,
                        action=action,
                        manifest_sha256=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
                    )
                ),
                permission_registered=True,
                adapter_registered=True,
            )
        )
    return CapabilityBindingSnapshot(bindings=tuple(bindings))


def operational_effect_capability_snapshot(
    *,
    settings: object,
    kernel: object,
    authorization: object,
    obsidian_runtime: object,
) -> CapabilityBindingSnapshot:
    """Bind P5 only to the enabled, registered Obsidian write contour.

    Unlike the read-capability snapshot, this identity is captured from the
    already composed production objects. No synthetic registry or import-level
    declaration can make the effect observer available: both create and append
    must be exact code-built tools registered in this kernel, backed by the
    exact registered permission and exact Obsidian runtime instance. The
    returned identity contains only closed symbols and digests.
    """

    try:
        from friday.execution_kernel import ExecutionKernel, ToolSpec
        from friday.orchestration.supervisor_effect_intent import (
            SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
        )
        from friday.organs import ServiceContext
        from friday.organs.obsidian import OBSIDIAN_WRITE
        from friday.organs.obsidian.runtime import ObsidianRuntime
        from friday.organs.obsidian.tools import build_obsidian_tools

        obsidian_enabled = getattr(settings, "obsidian_enabled", None)
        if (
            type(obsidian_enabled) is not bool
            or obsidian_enabled is not True
            or type(kernel) is not ExecutionKernel
            or type(authorization) is not AuthorizationService
            or type(obsidian_runtime) is not ObsidianRuntime
            or kernel.authorization is not authorization
            or kernel.settings is not settings
            or authorization.storage is None
            or kernel.storage is not authorization.storage
            or obsidian_runtime.settings is not settings
            or obsidian_runtime.storage is not authorization.storage
            or authorization.get_capability(_OBSIDIAN_EFFECT_SECURITY_ID) != OBSIDIAN_WRITE
        ):
            raise SupervisorContractError("Obsidian effect contour is not composed")

        effect_tool_names = {item[2] for item in _OBSIDIAN_EFFECT_CONTOURS}
        expected_tools = {
            tool.name: tool
            for tool in build_obsidian_tools(
                ServiceContext(
                    settings=settings,
                    storage=authorization.storage,
                    kg=kernel.kg,
                    ingestion=kernel.ingestion,
                    auth=authorization,
                    obsidian=obsidian_runtime,
                )
            )
            if tool.name in effect_tool_names
        }
        permission_digest = canonical_sha256(_permission_payload(OBSIDIAN_WRITE))
        bindings: list[OperationalCapabilityBinding] = []
        for capability_id, action, tool_name in _OBSIDIAN_EFFECT_CONTOURS:
            tool = kernel.get_tool(tool_name)
            expected = expected_tools.get(tool_name)
            if (
                type(tool) is not ToolSpec
                or type(expected) is not ToolSpec
                or tool.name != expected.name
                or tool.description != expected.description
                or tool.parameters != expected.parameters
                or tool.security_id != _OBSIDIAN_EFFECT_SECURITY_ID
                or tool.security_id != expected.security_id
                or tool.risk != _OBSIDIAN_EFFECT_TOOL_RISK
                or tool.risk != expected.risk
                or tool.allowed_execution_scopes != expected.allowed_execution_scopes
                or tool.timeout_sec != expected.timeout_sec
                or tool.model_visible is not True
                or tool.model_visible is not expected.model_visible
                or tool.approval_predicate is not expected.approval_predicate
                or not callable(tool.handler)
                or not callable(expected.handler)
                or tool.handler.__code__ is not expected.handler.__code__
                or _qualified_name(tool.handler) != _qualified_name(expected.handler)
                or not _handler_binds_runtime(tool.handler, obsidian_runtime)
            ):
                raise SupervisorContractError("Obsidian effect tool registry does not match")
            adapter_payload = _effect_tool_identity_payload(
                tool,
                action=action,
                manifest_sha256=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            )
            bindings.append(
                OperationalCapabilityBinding(
                    supervisor_capability_id=capability_id,
                    effect_class=CapabilityEffectClass.WRITE,
                    security_id=_OBSIDIAN_EFFECT_SECURITY_ID,
                    tool_id=tool_name,
                    adapter_id=_qualified_name(tool.handler),
                    permission_identity_sha256=permission_digest,
                    adapter_identity_sha256=canonical_sha256(adapter_payload),
                    permission_registered=True,
                    adapter_registered=True,
                )
            )
        snapshot = CapabilityBindingSnapshot(bindings=tuple(bindings))
        if len(snapshot.bindings) != len(_OBSIDIAN_EFFECT_CONTOURS) or not all(
            binding.available and binding.effect_class is CapabilityEffectClass.WRITE
            for binding in snapshot.bindings
        ):
            raise SupervisorContractError("Obsidian effect contour is incomplete")
        if snapshot.digest_hex() != expected_effect_capability_snapshot().digest_hex():
            raise SupervisorContractError("Obsidian effect contour identity does not match")
        return snapshot
    except SupervisorContractError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SupervisorContractError("Obsidian effect contour is unavailable") from exc


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
