"""Actual complete A009 physical snapshot projection; no own expected oracle."""
from common import Refused,canonical,domain


def observed_snapshot(ctx,manifest,event):
    ctx.verify_all_members(event["members"])
    ctx.verify_a009_final_domains(event["members"])
    physical={r["path"]:r for r in event["members"]}
    recipe=ctx.consumer("recipe_planner")
    members=[]
    for row in manifest["members"]:
        actual=physical.get(row["path"])
        if actual is None or actual["kind"]!=row["kind"] or actual["sha256"]!=row["sha256"] or actual["size"]!=row["size"]:
            raise Refused("complete_snapshot_physical_inventory")
        members.append({k:row[k] for k in ("executable","gid","kind","link_target","link_target_sha256",
            "mode","mount_domain","nlink","path","sha256","size","uid","device","parent_path")})
    fills=dict(ctx.ordinary.presented["snapshot_fields"])
    if fills["rootfs_sha256"] not in (None,manifest["rootfs_sha256"]):raise Refused("rootfs_identity")
    observed=ctx.call({"type":"snapshot-observed","abi":event["abi"]})
    # These are observed producer facts, not a replacement expected object or
    # golden. An independent final selection must later choose their full bytes.
    fills["creation_tool_sha256"]=observed["creation_tool_sha256"]
    recipe._compare_snapshot_identity(manifest["members"],fills)
    candidate={"broker_package_sha256":fills["broker_package_sha256"],"commit":ctx.expected["candidate"]["commit"],
        "controller_path":fills["controller_path"],"preflight_sha256":fills["preflight_sha256"],
        "quality_gate_sha256":fills["quality_gate_sha256"],"root_sha256":fills["root_sha256"],
        "tree":ctx.expected["candidate"]["tree"]}
    platform={"architecture":ctx.expected["platform"]["architecture"],"python_abi":ctx.expected["platform"]["python_abi"],
        "import_suffixes_sha256":fills["import_suffixes_sha256"],"kernel_contract_sha256":fills["kernel_contract_sha256"],
        "loader_contract_sha256":fills["loader_contract_sha256"],"rootfs_sha256":manifest["rootfs_sha256"]}
    members.sort(key=lambda r:(r["mount_domain"],r["path"].encode("utf-8")))
    recipe._close_links(members)
    approval=ctx.ordinary.presented["approval"]
    if approval is None:raise Refused("independent_full_approval_input_required")
    recipe_sha=domain("friday.lab822.recipe.v1",[{k:r[k] for k in ("destination","name","output_sha256","status")} for r in ctx.predecessors],ctx.meter)
    projection=ctx.consumer("authority").build_material_provenance(approval,ctx.expected,"NOT_PROVEN",
        ctx.consumer("ingress").load_pinned_schema("material-provenance.v1"),ctx.ordinary.presented["ubuntu_observations"],
        ctx.ordinary.presented["admitted_signers"],{"manifest_sha256":manifest["manifest_sha256"],"recipe_sha256":recipe_sha,
            "rootfs_sha256":manifest["rootfs_sha256"],"golden_sha256":ctx.ordinary.presented["golden_sha256"],
            "tool_sha256":observed["creation_tool_sha256"],"kernel_contract_sha256":fills["kernel_contract_sha256"]},
        ctx.ordinary.presented["held_member_pins"])
    identity={"issuer_id":ctx.ordinary.authority["approved_issuer_id"],"key_fingerprint":ctx.ordinary.authority["approved_fingerprint"]}
    materials_sha=ctx.consumer("authority").materials_digest(projection["artifacts"],identity,recipe_sha)
    snapshot={"schema":"snapshot-manifest.v1","contract":"a009","candidate":candidate,"platform":platform,
        "created_utc":observed["created_utc"],"creation_tool_sha256":observed["creation_tool_sha256"],
        "materials_sha256":materials_sha,"members":members,
        "root_merkle_sha256":domain("friday.lab820.snapshot-members.v1",members,ctx.meter),
        "runtime_contract_sha256":fills["runtime_contract_sha256"],"snapshot_id":None}
    snapshot["snapshot_id"]=domain("friday.lab820.snapshot-id.v1",{k:v for k,v in snapshot.items() if k!="snapshot_id"},ctx.meter)
    ctx.consumer("schema_validate").validate_document(snapshot,ctx.consumer("ingress").load_pinned_schema("snapshot-manifest.v1"))
    ctx.put_body("full-a009-snapshot",snapshot)
    return snapshot
