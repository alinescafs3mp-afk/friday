"""Bounded semantic catalog for reviewed host capabilities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .adapters.base import AdapterSpec
from .adapters.jq import JQ_SPEC
from .adapters.nmap import NMAP_SPEC
from .contracts import AdapterState, ContractError, canonical_digest

MAX_CATALOG_RESULTS = 8


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    capability_id: str
    adapter_id: str
    state: AdapterState
    summary: str
    categories: tuple[str, ...]
    actions: tuple[str, ...]
    package_candidate_ref: str | None = None
    attestation_digest: str | None = None

    def __post_init__(self) -> None:
        token = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
        if not token.fullmatch(self.capability_id) or not token.fullmatch(self.adapter_id):
            raise ContractError("catalog capability identity is invalid")
        if not self.summary or len(self.summary) > 240 or not self.actions:
            raise ContractError("catalog capability description is invalid")
        if self.package_candidate_ref is not None and not re.fullmatch(
            r"^candidate_[0-9a-f]{16,64}$", self.package_candidate_ref
        ):
            raise ContractError("catalog package candidate reference is invalid")
        if self.attestation_digest is not None and not re.fullmatch(
            r"^[0-9a-f]{64}$", self.attestation_digest
        ):
            raise ContractError("catalog attestation digest is invalid")
        if self.state is AdapterState.AVAILABLE and self.attestation_digest is None:
            raise ContractError("available capability lacks executable attestation")

    def to_public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actions": list(self.actions),
            "adapter_id": self.adapter_id,
            "capability_id": self.capability_id,
            "categories": list(self.categories),
            "state": self.state.value,
            "summary": self.summary,
        }
        if self.package_candidate_ref is not None:
            payload["package_candidate_ref"] = self.package_candidate_ref
        return payload


class CapabilityCatalog:
    def __init__(self, adapters: tuple[AdapterSpec, ...]) -> None:
        if not adapters or len(adapters) > 64:
            raise ContractError("adapter catalog is empty or oversized")
        if len({item.adapter_id for item in adapters}) != len(adapters):
            raise ContractError("adapter catalog ids are not unique")
        self._adapters = adapters

    @property
    def adapters(self) -> tuple[AdapterSpec, ...]:
        return self._adapters

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "adapters": [
                    {"adapter_id": item.adapter_id, "digest": item.digest} for item in self._adapters
                ],
                "schema_version": 1,
            }
        )

    def entries(
        self,
        *,
        adapter_states: dict[str, AdapterState],
        attestation_digests: dict[str, str] | None = None,
        candidate_refs: dict[str, str] | None = None,
    ) -> tuple[CapabilityEntry, ...]:
        attestations = attestation_digests or {}
        candidates = candidate_refs or {}
        entries: list[CapabilityEntry] = []
        for adapter in self._adapters:
            try:
                state = AdapterState(adapter_states.get(adapter.adapter_id, AdapterState.UNATTESTED))
            except ValueError as exc:
                raise ContractError("adapter catalog state is invalid") from exc
            grouped: dict[str, list[str]] = {}
            for action in adapter.actions:
                grouped.setdefault(action.capability_id, []).append(action.action_id)
            for capability_id, actions in grouped.items():
                entries.append(
                    CapabilityEntry(
                        capability_id=capability_id,
                        adapter_id=adapter.adapter_id,
                        state=state,
                        summary=adapter.summary,
                        categories=adapter.categories,
                        actions=tuple(actions),
                        package_candidate_ref=candidates.get(adapter.adapter_id)
                        if state is AdapterState.MISSING_PACKAGE
                        else None,
                        attestation_digest=attestations.get(adapter.adapter_id)
                        if state is AdapterState.AVAILABLE
                        else None,
                    )
                )
        return tuple(entries)

    def search(
        self,
        query: str,
        *,
        entries: tuple[CapabilityEntry, ...],
        category: str | None = None,
        limit: int = MAX_CATALOG_RESULTS,
    ) -> tuple[CapabilityEntry, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_CATALOG_RESULTS:
            raise ContractError("catalog search limit is invalid")
        normalized_query = " ".join(str(query or "").casefold().split())[:240]
        terms = tuple(dict.fromkeys(re.findall(r"[a-zа-яё0-9_.-]{2,40}", normalized_query)))[:16]
        normalized_category = str(category or "").strip().casefold()
        scored: list[tuple[int, str, CapabilityEntry]] = []
        for entry in entries:
            if normalized_category and normalized_category not in entry.categories:
                continue
            haystack = " ".join(
                (entry.capability_id, entry.adapter_id, entry.summary, *entry.categories, *entry.actions)
            ).casefold()
            score = sum(3 if term in entry.capability_id else 1 for term in terms if term in haystack)
            if terms and not score:
                continue
            scored.append((score, entry.capability_id, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored[:limit])

    def describe(self, capability_id: str, *, entries: tuple[CapabilityEntry, ...]) -> CapabilityEntry:
        found = next((item for item in entries if item.capability_id == capability_id), None)
        if found is None:
            raise ContractError("host capability is unknown")
        return found


BUILTIN_CATALOG = CapabilityCatalog((NMAP_SPEC, JQ_SPEC))

__all__ = [
    "BUILTIN_CATALOG",
    "MAX_CATALOG_RESULTS",
    "CapabilityCatalog",
    "CapabilityEntry",
]
