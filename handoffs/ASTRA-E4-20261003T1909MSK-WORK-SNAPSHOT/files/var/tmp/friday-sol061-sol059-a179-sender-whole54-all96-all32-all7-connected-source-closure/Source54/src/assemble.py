"""Deterministic static approved archive assembly in private quarantine.

No package script, binary, interpreter startup file or subprocess is executed.
Installed read-only/root ownership is a separate grant-bound installer effect.
"""
import hashlib
import os
from canonical import ContractError, digest, validate_digest
from archive import ArchivePlan, validate_members, materialized_payloads
from pinned_fs import PinnedRoot
from provenance import ApprovedMaterials, require_approved_materials

ASSEMBLY_EFFECTS = ("directory_create", "file_create", "hardlink_copy", "symlink_create",
                    "inventory_verify")


def assemble(root, plan, *, expected_plan_sha256, approved_materials, fixture_authority=False):
    validate_digest(expected_plan_sha256)
    if not isinstance(root, PinnedRoot) or not isinstance(plan, ArchivePlan) or not isinstance(approved_materials, ApprovedMaterials):
        raise ContractError("held root, validated plan and external materials required")
    require_approved_materials(approved_materials, fixture_authority=fixture_authority)
    if fixture_authority and (os.geteuid() == 0 or root.expected_uid not in (None, os.getuid())):
        raise ContractError("fixture assembly is non-root private-source only")
    if digest(plan.projection()) != expected_plan_sha256 or plan.plan_sha256 != expected_plan_sha256:
        raise ContractError("external assembly plan mismatch")
    verified = validate_members([dict(item) for item in plan.members], payloads=dict(plan.payloads),
                                source_sha256=plan.source_sha256, limits=dict(plan.limits))
    if verified.plan_sha256 != expected_plan_sha256:
        raise ContractError("mutated archive plan")
    payloads, materialized_bytes = materialized_payloads(verified)
    if set(plan.payloads) != {member["path"] for member in plan.members if member["type"] == "file"}:
        raise ContractError("complete assembly payload inventory required")
    if plan.source_sha256 not in {hashlib.sha256(raw).hexdigest() for raw in approved_materials.artifacts.values()}:
        raise ContractError("archive bytes not externally approved")
    validate_digest(approved_materials.provenance_sha256)
    validate_digest(approved_materials.assembly_recipe_sha256)
    validate_digest(approved_materials.creation_tool_sha256)
    if root.walk_exact():
        raise ContractError("quarantine must be fresh/empty")
    for member in sorted(plan.members, key=lambda item: (item["path"].count("/"), item["path"])):
        if member["type"] == "directory":
            root.backend.event("pre:assembly:directory_create", member["path"])
            root.mkdir_new(member["path"], mode=0o700)
            root.backend.event("post:assembly:directory_create", member["path"])
        elif member["type"] == "file":
            root.backend.event("pre:assembly:file_create", member["path"])
            root.write_new(member["path"], payloads[member["path"]], mode=0o600)
            root.backend.event("post:assembly:file_create", member["path"])
    # Links follow all directories/files; hardlinks become independent exact
    # copies, preserving the snapshot invariant that every regular file nlink=1.
    for member in plan.members:
        if member["type"] == "hardlink":
            root.backend.event("pre:assembly:hardlink_copy", member["path"])
            root.write_new(member["path"], payloads[member["path"]], mode=0o600)
            root.backend.event("post:assembly:hardlink_copy", member["path"])
        elif member["type"] == "symlink":
            root.backend.event("pre:assembly:symlink_create", member["path"])
            root.symlink_new(member["path"], member["target"])
            root.backend.event("post:assembly:symlink_create", member["path"])
    root.backend.event("pre:assembly:inventory_verify", "")
    inventory = root.walk_exact()
    expected = {member["path"]: member for member in plan.members}
    if {member["path"] for member in inventory} != set(expected):
        raise ContractError("assembled inventory missing/extra")
    if sum(item.get("size", 0) for item in inventory if item["type"] == "file") != materialized_bytes or materialized_bytes > plan.limits["total_bytes"]:
        raise ContractError("assembled materialized byte budget mismatch")
    for actual in inventory:
        desired = expected[actual["path"]]
        desired_type = "file" if desired["type"] == "hardlink" else desired["type"]
        if actual["type"] != desired_type:
            raise ContractError("assembled member type")
        if desired_type == "file":
            raw = payloads[desired["path"]]
            if actual["size"] != len(raw) or actual["sha256"] != hashlib.sha256(raw).hexdigest() or actual["nlink"] != 1 or actual["mode"] != 0o600:
                raise ContractError("assembled file bytes/mode/link mismatch")
        elif desired_type == "directory" and actual["mode"] != 0o700:
            raise ContractError("assembled private directory mode")
        elif desired_type == "symlink" and actual["target"] != desired["target"]:
            raise ContractError("assembled link target")
    root.backend.event("post:assembly:inventory_verify", "")
    return {"inventory": inventory, "inventory_sha256": digest(inventory),
            "plan_sha256": expected_plan_sha256, "provenance_sha256": approved_materials.provenance_sha256,
            "materials_sha256": approved_materials.materials_sha256,
            "authority_mode": approved_materials.authority_mode, "materialized_bytes": materialized_bytes}


def assemble_synthetic(root, plan, *, expected_plan_sha256, approved_materials):
    """Explicit fixture authority; result never grants native installation."""
    return assemble(root, plan, expected_plan_sha256=expected_plan_sha256,
                    approved_materials=approved_materials, fixture_authority=True)
