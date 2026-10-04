"""Actual Root producer normalization into the UNCHANGED exact A128 ABI.

The Root parent supplies kernel/FD/process/stream observations from its owned
launch. None of these fields is accepted from the performer as Root authority.
Capabilities are non-grant structural records for the separate A128 consumer.
"""
import os
import stat
from common import Refused, INPUT_MAX, DOCUMENT_MAX, exact, canonical, domain, sha, mono
from custody import Held, identity9

ISSUERS={"wheel":"pypi-retained","native":"system-native","browser":"browser-publisher",
    "data":"system-data","candidate":"owner-candidate","golden":"owner-golden",
    "kernel":"owner-kernel","unrar":"rarlab-publisher","member":"independent-member",
    "custody":"independent-custody","ubuntu-inrelease":"ubuntu-archive",
    "ubuntu-packages":"ubuntu-archive","ubuntu-archive":"ubuntu-archive",
    "node-archive":"nodejs.org","node-shasums256":"nodejs.org",
    "data-closure":"system-data","native-closure":"system-native",
    "snapshot":"independent-snapshot","operation":"independent-operation"}
METHODS={"authenticate-ubuntu-indexes":"index-selection","authenticate-ubuntu-archives":"archive-install",
    "authenticate-node-archive":"node-install","hold-unrar-publisher-gap":"unrar-gap",
    "authenticate-wheels":"wheel-install","map-cpython-venv":"venv-map","map-lib-dynload":"dynload-map",
    "map-native-loader":"loader-map","map-browser-resources":"browser-install","map-data-closure":"data-map",
    "bind-candidate":"candidate-tree","bind-golden":"golden-tree","assemble-members":"assemble",
    "write-final-manifest":"manifest","external-custody":"custody"}
CAPABILITY_FIELDS=("schema","issuer_id","actor_id","document_kind","method","target",
    "artifact_sha256","input_sha256","dependencies_sha256","abi_sha256","resources_sha256",
    "tool_sha256","environment_sha256","custody_sha256","produced_by_this_package","effects_granted")
BODY_FIELDS=("producer_id","target","input_body","members","dependencies","abi","resources",
             "runtime","custody","output_paths","package_bindings")


class RootNormalizer:
    def __init__(self, parent):
        self.parent=parent
        self.counter=0

    def member(self, fact):
        """Independently read/stat/hash the actual output; do not trust fact counts."""
        p=self.parent
        path=fact["path"]
        p.check_owned_path(fact["physical_path"])
        s=os.lstat(fact["physical_path"])
        kind="regular" if stat.S_ISREG(s.st_mode) else "directory" if stat.S_ISDIR(s.st_mode) else "symlink" if stat.S_ISLNK(s.st_mode) else None
        if kind!=fact["kind"]:raise Refused("actual_member_kind")
        content=target=target_sha=ref=None
        size=s.st_size if kind=="regular" else 0
        if kind=="regular":
            pin={"path":fact["physical_path"],"bytes":size,"sha256":fact["sha256"],
                 "identity9_decimal_strings":identity9(s)}
            with Held(pin["path"],pin,p.observer,DOCUMENT_MAX) as held:
                # Same held complete body is retained into a closed packed page.
                ref=p.pack_preimage(held,"member")
                content=held.body_sha
        elif kind=="symlink":
            target=os.readlink(fact["physical_path"])
            if target!=fact["link_target"]:raise Refused("actual_link_target")
            target_sha=sha(target.encode("utf-8"),p.observer)
        roots=("/opt/friday/quality-toolchain/venv","/work/candidate","/inputs/golden")
        parent="" if path in roots else path.rsplit("/",1)[0] if "/" in path else ""
        mount=next((r for r in roots if path==r or path.startswith(r+"/")),"/")
        mode=format(stat.S_IMODE(s.st_mode),"04o")
        if mode!=("0555" if kind=="directory" or kind=="regular" and fact["executable"] else "0444" if kind=="regular" else "0777"):
            raise Refused("actual_member_mode")
        if s.st_uid>65535 or s.st_gid>65535 or kind=="regular" and s.st_nlink!=1:
            raise Refused("actual_member_owner")
        return {"operation":fact["operation"],"path":path,"kind":kind,"mode":mode,"size":size,
            "uid":s.st_uid,"gid":s.st_gid,"nlink":s.st_nlink,"device":s.st_dev,
            "mount_domain":mount,"parent_path":parent,"link_target":target,
            "link_target_sha256":target_sha,"content_sha256":content,
            "status":"STRUCTURALLY_BOUND","source_ref":ref}

    def normalize(self, section, target, event):
        p=self.parent
        if section not in ("materials","operations","closures","snapshot"):raise Refused("section")
        kind=("operation" if section=="operations" else "snapshot" if section=="snapshot"
              else target+"-closure" if section=="closures" else "custody" if target=="a009-custody" else target.split("/",1)[0])
        if kind not in ISSUERS:raise Refused("document_kind")
        method=METHODS[target] if section=="operations" else kind
        if event["status"]!="COMPLETED":raise Refused("performing_event")
        if section=="operations" and target not in p.operation_windows:
            raise Refused("root_operation_window")
        # Every class authentication receipt is joined to the actual Root native
        # invocation table, full tool/image pins and selected input body hashes.
        p.verify_semantic_event(section,target,event)
        p.pack_member_batch(event["members"])
        members=[self.member(row) for row in event["members"]]
        if not 1<=len(members)<=512:raise Refused("member_limit")
        if p.consumer("whole_join").bind_member_hierarchy(members)["closed"] is not True:
            raise Refused("actual_member_hierarchy")
        producer=p.actor_id
        body={"producer_id":producer,"target":target,"input_body":event["input_body"],
            "members":members,"dependencies":event["dependencies"],"abi":event["abi"],
            "resources":event["resources"],"runtime":None,"custody":None,
            "output_paths":event["output_paths"],"package_bindings":event["package_bindings"]}
        p.verify_domains(body,event)
        tool=p.actual_tool(body["abi"])
        stdout,stderr=p.actual_operation_streams(target)
        begin=p.operation_windows.get(target,{"started_ns":p.actor_started_ns})["started_ns"]
        finish=mono()
        environment={"producer_id":producer,"entries":[{"name":k,"value":v} for k,v in sorted(p.actor_env.items())],
            "kernel":p.actual_kernel(),"process":{"pid":p.actor_pid,"started_ns":p.actor_started_ns,
                "uid":p.actor_uid,"gid":p.actor_gid},"observed_at_ns":mono()}
        hashes={"input_sha256":domain("friday.a117.performing-input.v1",body["input_body"],p.observer),
            "member_inventory_sha256":domain("friday.a117.installed-members.v1",members,p.observer),
            "dependencies_sha256":domain("friday.a128.dependencies.v1",body["dependencies"],p.observer),
            "abi_sha256":domain("friday.a117.abi.v1",body["abi"],p.observer),
            "resources_sha256":domain("friday.a117.resources.v1",body["resources"],p.observer),
            "tool_sha256":domain("friday.a128.full-tool.v1",tool,p.observer),
            "environment_sha256":domain("friday.a128.full-environment.v1",environment,p.observer)}
        observation={"producer_id":producer,"target":target,"method":method,**hashes,
            "exit_code":0,"stdout":stdout,"stderr":stderr,"started_ns":begin,"finished_ns":finish,
            "derivations":[{"path":r["path"],"method":method,"source_ref":r["source_ref"],
                "source_size":r["size"] if r["kind"]=="regular" else 0,
                "selector":{"input_sha256":hashes["input_sha256"],"output_path":r["path"],
                    "output_sha256":r["content_sha256"],"output_size":r["size"]}}
                for r in members if r["path"] in body["output_paths"]],
            "aggregate_resources":p.observer.a128_aggregate()}
        raw_custody={"producer_id":producer,"target":target,"root_launch":p.actual_launch_fact,
            "admission_ref":p.qualification["raw_ref"],"same_held_input_refs":p.input_preimages(target),
            "member_inventory":members,"native_receipts":p.native_receipts_for(target),
            "image_pin":p.admission["image"]["manifest_pin"],"effects_granted":False}
        custody_record=p.store.put("custody-"+str(self.counter),canonical(raw_custody,p.observer),"custody")
        custody_sha=domain("friday.a138.root-actor-physical-custody.v1",raw_custody,p.observer)
        artifact=(event["input_body"]["sha256"] if section=="materials" and "sha256" in event["input_body"]
                  else domain("friday.a128.operation-artifact.v1",body["input_body"],p.observer))
        cap={"schema":"friday.a128.class-capability.v1","issuer_id":ISSUERS[kind],
            "actor_id":producer,"document_kind":kind,"method":method,"target":target,
            "artifact_sha256":artifact,
            **{k:hashes[k] for k in ("input_sha256","dependencies_sha256","abi_sha256",
                "resources_sha256","tool_sha256","environment_sha256")},
            "custody_sha256":custody_sha,"produced_by_this_package":False,"effects_granted":False}
        exact(cap,CAPABILITY_FIELDS,"a128_exact16")
        from roles import validate_role
        validate_role(cap,"class_capability",p.enrollment,p.observer)
        obs_sha=domain("friday.a128.full-runtime-observation.v1",observation,p.observer)
        result={"schema":"friday.a128.class-result.v1","issuer_id":ISSUERS[kind],"actor_id":producer,
            "capability_sha256":domain("friday.a128.class-capability.v1",cap,p.observer),
            "observation_sha256":obs_sha,"artifact_sha256":artifact,
            "member_inventory_sha256":hashes["member_inventory_sha256"],"custody_sha256":custody_sha,
            "decision":"STRUCTURALLY_BOUND","produced_by_this_package":False}
        runtime={"producer_id":producer,"artifact_sha256":artifact,"tool_sha256":hashes["tool_sha256"],
            "environment_sha256":hashes["environment_sha256"],"observation_sha256":obs_sha,
            "tool":tool,"environment":environment,"observation":observation,"capability":cap,"verification_result":result}
        body["runtime"]=runtime
        body["custody"]={"producer_id":producer,"target":target,"input_sha256":hashes["input_sha256"],
            "member_inventory_sha256":hashes["member_inventory_sha256"],
            "runtime_sha256":domain("friday.a117.runtime-observation.v1",runtime,p.observer),
            "resources_sha256":hashes["resources_sha256"],"selected_by":p.selector_id,"effects_granted":False}
        exact(body,BODY_FIELDS,"a128_exact11")
        raw=canonical(body,p.observer)
        record=p.store.put("performing-"+str(self.counter),raw,"member")
        self.counter+=1
        # Independent expected bytes are NEVER computed here. The external
        # owner selects its full bytes before this receipt is consumed; it may
        # do so after actual retention, before any dependent public hash.
        try:selected=p.external_expected(section,target)
        except Refused as exc:
            if exc.cause!="independent_full_expected_body_required":raise
            selected=None
        receipt={"body":body,"record":record,"custody_record":custody_record,
            "section":section,"target":target,"expected":selected,"raw_sha256":sha(raw,p.observer),
            "capability_fields":16,"performing_body_fields":11,
            "current_grant":False,"Source_issued_grant":False,
            "independent_selection":"REQUIRED_NOT_RUN","retained_ns":mono()}
        p.retained_receipts.append({k:v for k,v in receipt.items() if k!="body"})
        if p.material_phase and section in ("materials","closures"):
            # Full material-phase selection is one signed complete collection,
            # not hundreds of per-record helpers violating the original ABI128.
            receipt["expected"]=None
            p.retained_receipts[-1]["expected"]=None
            receipt["independent_selection"]="PENDING_COMPLETE_PHASE_SELECTION"
            return receipt
        if selected is None or selected["pin"]["sha256"]!=record["pin"]["sha256"]:
            from independent_selector import selected_after_retention
            selected=selected_after_retention(p,section,target,record["pin"],self.counter-1)
            receipt["expected"]=selected
            p.retained_receipts[-1]["expected"]=selected
        if selected is None:return receipt
        with Held(selected["pin"]["path"],selected["pin"],p.observer,INPUT_MAX,True) as expected:
            expected_raw=expected.read(INPUT_MAX)
            if expected_raw!=raw:raise Refused("independent_full_observation_mismatch","postdelivery")
        receipt["independent_selection"]="EXTERNALLY_SELECTED_FULL_BYTES_MATCH"
        return receipt
