"""Decision-complete human summaries for exact Host Control approvals."""

from __future__ import annotations

from collections.abc import Iterable

from friday_package_broker.contracts import AptInstallPlan, PackageAction

from .adapters.nmap import SERVICE_PORTS
from .contracts import ContractError
from .plans import HostActionPlan
from .policy import NetworkTargetSnapshot

_MAX_APPROVAL_SUMMARY_CHARS = 64 * 1024


def _plain(value: object, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(character for character in text if character.isprintable())
    return text[:maximum]


def _closed_summary(lines: Iterable[str]) -> str:
    summary = "\n".join(str(line).rstrip() for line in lines if str(line).strip()).strip()
    if not summary or len(summary) > _MAX_APPROVAL_SUMMARY_CHARS:
        raise ContractError("approval summary exceeds the human decision envelope")
    return summary


def package_install_approval_summary(
    plan: AptInstallPlan,
    *,
    expected_capabilities: tuple[str, ...],
    original_request: str,
) -> str:
    """Render every effect of one exact APT transaction without ellipsis."""

    transaction = plan.transaction
    if len(transaction.changes) > 32:
        # The shipped broker policy is stricter than the wire contract. If that
        # policy ever drifts, refuse to ask a person to approve an unreadable plan.
        raise ContractError("package approval contains too many changes")
    requested = ", ".join(f"{item.name}={item.version}:{item.architecture}" for item in transaction.requested)
    action_labels = {
        PackageAction.INSTALL: "ADD",
        PackageAction.UPGRADE: "UPGRADE",
        PackageAction.DOWNGRADE: "DOWNGRADE",
        PackageAction.REINSTALL: "REINSTALL",
        PackageAction.REMOVE: "REMOVE",
    }
    change_lines: list[str] = []
    for change in transaction.changes:
        before = change.from_version or "∅"
        after = change.to_version or "∅"
        change_lines.append(
            f"- {action_labels[change.action]} {change.name}:{change.architecture} {before} -> {after}; "
            f"download={change.download_bytes}; disk_delta={change.installed_delta_bytes}; "
            f"archive_sha256={change.archive_sha256 or 'none'}"
        )
    if not change_lines:
        change_lines.append("- NO PACKAGE STATE CHANGE")

    origin_rows = {
        (
            origin.origin,
            origin.label,
            origin.site,
            origin.archive,
            origin.component,
            origin.trusted,
        )
        for change in transaction.changes
        for origin in change.origins
    }
    if len(origin_rows) > 64:
        raise ContractError("package approval contains too many repository origins")
    origin_lines = [
        "- "
        + "; ".join(
            (
                f"origin={_plain(origin, maximum=160) or '-'}",
                f"label={_plain(label, maximum=160) or '-'}",
                f"site={_plain(site, maximum=160) or '-'}",
                f"archive={_plain(archive, maximum=160) or '-'}",
                f"component={_plain(component, maximum=160) or '-'}",
                f"trusted={'yes' if trusted else 'NO'}",
            )
        )
        for origin, label, site, archive, component, trusted in sorted(origin_rows)
    ] or ["- none (valid only when the exact plan contains removals or no changes)"]
    warning_lines = [f"- {_plain(item, maximum=240)}" for item in transaction.warnings] or ["- none"]
    capability_lines = [f"- {_plain(item, maximum=128)}" for item in expected_capabilities] or ["- none"]
    return _closed_summary(
        (
            "Package manager: APT",
            f"Exact requested package(s): {requested}",
            "Exact package changes:",
            *change_lines,
            "Configured repository origins:",
            *origin_lines,
            f"Download bytes: {transaction.download_bytes}",
            f"Estimated disk change bytes: {transaction.installed_delta_bytes}",
            "Services/units at plan time: not detectable from this APT plan; the receipt will record "
            "bounded before/after observations.",
            "Friday capabilities expected after attestation:",
            *capability_lines,
            "APT warnings:",
            *warning_lines,
            f"Original task to resume: {_plain(original_request, maximum=1000) or plan.original_task_ref}",
            f"Original task ref: {plan.original_task_ref}",
            f"Continuation work item: {plan.continuation_work_item_id}",
            f"Plan expires (unix): {plan.expires_at}",
            f"Exact plan sha256: {plan.digest}",
        )
    )


def host_action_approval_summary(plan: HostActionPlan) -> str:
    """Render the pinned network scope and execution envelope of one action."""

    if plan.target_snapshot is None:
        raise ContractError("network approval lacks an exact target snapshot")
    snapshot = NetworkTargetSnapshot.from_payload(plan.target_snapshot)
    binding_lines: list[str] = []
    for binding in snapshot.bindings:
        binding_lines.append(
            f"- requested={binding.requested}; pinned={','.join(binding.execution_targets)}; "
            f"resolved={','.join(binding.resolved_addresses) or 'n/a'}; "
            f"classification={binding.classification}; addresses={binding.address_count}; "
            f"route={','.join(binding.route_evidence) or 'none'}"
        )

    arguments = plan.normalized_arguments
    if plan.adapter_id == "network.nmap":
        if plan.action_id == "discover":
            profile = "nmap discovery (-sn), no service ports"
        elif plan.action_id == "services":
            profile = "nmap TCP connect/version-light ports=" + ",".join(str(item) for item in SERVICE_PORTS)
        elif plan.action_id == "selected_ports":
            raw_ports = arguments.get("ports")
            if not isinstance(raw_ports, list) or not raw_ports:
                raise ContractError("selected-port approval lacks exact ports")
            profile = "nmap TCP connect/version-light ports=" + ",".join(str(item) for item in raw_ports)
        else:
            raise ContractError("nmap approval action is unsupported")
    else:
        profile = f"{plan.adapter_id}/{plan.action_id}"

    return _closed_summary(
        (
            f"Host action: {plan.capability_id} via {plan.adapter_id}/{plan.action_id}",
            "Exact target bindings:",
            *binding_lines,
            f"Execution profile: {profile}",
            f"Pinned target count: {snapshot.target_count}",
            f"Expected coverage: account for all {snapshot.target_count} pinned addresses; partial/unknown "
            "must be reported explicitly.",
            f"Timeout seconds: {plan.timeout_sec}",
            f"Maximum captured output bytes: {plan.max_output_bytes}",
            f"Sandbox profile: {plan.execution_profile.value}",
            f"Host agent: {plan.host_agent_id}",
            f"Adapter digest: {plan.adapter_digest}",
            f"Executable attestation digest: {plan.executable_attestation_digest}",
            f"Network policy digest: {snapshot.policy_digest}",
            f"Source message: {plan.source_message_id}",
            f"Plan expires (unix): {plan.expires_at}",
            f"Exact plan sha256: {plan.digest}",
        )
    )


__all__ = ["host_action_approval_summary", "package_install_approval_summary"]
