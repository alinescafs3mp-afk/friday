"""Code-owned capability catalog and the bounded per-turn manifest projection."""

from __future__ import annotations

from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    bind_manifest_to_snapshot,
    operational_capability_snapshot,
)
from friday.orchestration.contracts import TurnInput
from friday.orchestration.supervisor_contracts import (
    ARCHIVE_SEARCH_ID,
    ARCHIVE_SEARCH_INPUT_SCHEMA,
    CAPABILITY_OUTCOME_SCHEMA_ID,
    CONVERSATION_WINDOW_INPUT_SCHEMA,
    CONVERSATION_WINDOW_READ_ID,
    FILE_CURRENT_READ_ID,
    FILE_CURRENT_READ_INPUT_SCHEMA,
    HOST_SCAN_LOCAL_ID,
    KNOWLEDGE_WRITE_ID,
    PRIMARY_SYNTHESIS_ID,
    SECONDARY_SUPERVISOR_ID,
    WEB_SEARCH_CURRENT_ID,
    WEB_SEARCH_CURRENT_INPUT_SCHEMA,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityEffectClass,
    CapabilityManifest,
    ModelRoleDescriptor,
)

_FILE_CURRENT_READ = CapabilityDescriptor(
    id=FILE_CURRENT_READ_ID,
    effect_class=CapabilityEffectClass.READ,
    input_schema_id=FILE_CURRENT_READ_INPUT_SCHEMA,
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.AVAILABLE,
    semantic_tags=("file", "attachment", "current"),
    max_items=16,
    supports_date_filter=False,
    supports_exact_replay=True,
)
_ARCHIVE_SEARCH = CapabilityDescriptor(
    id=ARCHIVE_SEARCH_ID,
    effect_class=CapabilityEffectClass.READ,
    input_schema_id=ARCHIVE_SEARCH_INPUT_SCHEMA,
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.AVAILABLE,
    semantic_tags=("archive", "messages", "documents"),
    max_items=20,
    supports_date_filter=True,
    supports_exact_replay=False,
)
_WEB_SEARCH_CURRENT = CapabilityDescriptor(
    id=WEB_SEARCH_CURRENT_ID,
    effect_class=CapabilityEffectClass.READ,
    input_schema_id=WEB_SEARCH_CURRENT_INPUT_SCHEMA,
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.AVAILABLE,
    semantic_tags=("web", "current", "public"),
    max_items=8,
    supports_date_filter=True,
    supports_exact_replay=False,
)
_CONVERSATION_WINDOW = CapabilityDescriptor(
    id=CONVERSATION_WINDOW_READ_ID,
    effect_class=CapabilityEffectClass.READ,
    input_schema_id=CONVERSATION_WINDOW_INPUT_SCHEMA,
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.AVAILABLE,
    semantic_tags=("conversation", "window"),
    max_items=1,
    supports_date_filter=False,
    supports_exact_replay=True,
)
_KNOWLEDGE_WRITE = CapabilityDescriptor(
    id=KNOWLEDGE_WRITE_ID,
    effect_class=CapabilityEffectClass.WRITE,
    input_schema_id="friday.knowledge-write-input.v1",
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.UNAVAILABLE,
    semantic_tags=("knowledge", "write"),
    max_items=1,
    supports_date_filter=False,
    supports_exact_replay=False,
)
_HOST_SCAN_LOCAL = CapabilityDescriptor(
    id=HOST_SCAN_LOCAL_ID,
    effect_class=CapabilityEffectClass.HIGH,
    input_schema_id="friday.host-scan-local-input.v1",
    output_schema_id=CAPABILITY_OUTCOME_SCHEMA_ID,
    availability=CapabilityAvailability.UNAVAILABLE,
    semantic_tags=("network", "scan"),
    max_items=1,
    supports_date_filter=False,
    supports_exact_replay=False,
)

CODE_OWNED_CAPABILITY_CATALOG: tuple[CapabilityDescriptor, ...] = (
    _FILE_CURRENT_READ,
    _ARCHIVE_SEARCH,
    _WEB_SEARCH_CURRENT,
    _CONVERSATION_WINDOW,
    _KNOWLEDGE_WRITE,
    _HOST_SCAN_LOCAL,
)
CODE_OWNED_MODEL_ROLES: tuple[ModelRoleDescriptor, ...] = (
    ModelRoleDescriptor(
        id=PRIMARY_SYNTHESIS_ID,
        availability=CapabilityAvailability.AVAILABLE,
        semantic_tags=("dialogue", "russian", "final_synthesis"),
    ),
    ModelRoleDescriptor(
        id=SECONDARY_SUPERVISOR_ID,
        availability=CapabilityAvailability.AVAILABLE,
        semantic_tags=("planning", "classification", "critique"),
    ),
)


def _with_availability(
    descriptor: CapabilityDescriptor,
    availability: CapabilityAvailability,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=descriptor.id,
        effect_class=descriptor.effect_class,
        input_schema_id=descriptor.input_schema_id,
        output_schema_id=descriptor.output_schema_id,
        availability=availability,
        semantic_tags=descriptor.semantic_tags,
        max_items=descriptor.max_items,
        supports_date_filter=descriptor.supports_date_filter,
        supports_exact_replay=descriptor.supports_exact_replay,
    )


def _bound_availability(
    capability_id: str,
    local_availability: CapabilityAvailability,
    snapshot: CapabilityBindingSnapshot,
) -> CapabilityAvailability:
    binding = snapshot.binding_for(capability_id)
    if binding is None or not binding.available:
        return CapabilityAvailability.UNAVAILABLE
    return local_availability


def bounded_capability_manifest(
    turn: TurnInput,
    *,
    binding_snapshot: CapabilityBindingSnapshot | None = None,
) -> CapabilityManifest:
    """Project only the read capabilities the current turn can honestly use."""

    snapshot = binding_snapshot or operational_capability_snapshot()
    selected: list[CapabilityDescriptor] = []
    if turn.attachments:
        local_availability = (
            CapabilityAvailability.AVAILABLE
            if any(item.extracted_text_available for item in turn.attachments)
            else CapabilityAvailability.PARTIAL
        )
        selected.append(
            _with_availability(
                _FILE_CURRENT_READ,
                _bound_availability(FILE_CURRENT_READ_ID, local_availability, snapshot),
            )
        )
    if turn.conversation_present:
        selected.append(
            _with_availability(
                _CONVERSATION_WINDOW,
                _bound_availability(
                    CONVERSATION_WINDOW_READ_ID,
                    CapabilityAvailability.AVAILABLE,
                    snapshot,
                ),
            )
        )
        selected.append(
            _with_availability(
                _ARCHIVE_SEARCH,
                _bound_availability(
                    ARCHIVE_SEARCH_ID,
                    CapabilityAvailability.AVAILABLE,
                    snapshot,
                ),
            )
        )
    if turn.enable_tools:
        selected.append(
            _with_availability(
                _WEB_SEARCH_CURRENT,
                _bound_availability(
                    WEB_SEARCH_CURRENT_ID,
                    CapabilityAvailability.AVAILABLE,
                    snapshot,
                ),
            )
        )
    if not selected:
        selected.append(
            _with_availability(
                _CONVERSATION_WINDOW,
                _bound_availability(
                    CONVERSATION_WINDOW_READ_ID,
                    CapabilityAvailability.AVAILABLE,
                    snapshot,
                ),
            )
        )
    public_manifest = CapabilityManifest.from_parts(selected, CODE_OWNED_MODEL_ROLES)
    return bind_manifest_to_snapshot(public_manifest, snapshot)


def catalog_capability(capability_id: str) -> CapabilityDescriptor | None:
    for item in CODE_OWNED_CAPABILITY_CATALOG:
        if item.id == capability_id:
            return item
    return None


def catalog_ids() -> tuple[str, ...]:
    return tuple(item.id for item in CODE_OWNED_CAPABILITY_CATALOG)
