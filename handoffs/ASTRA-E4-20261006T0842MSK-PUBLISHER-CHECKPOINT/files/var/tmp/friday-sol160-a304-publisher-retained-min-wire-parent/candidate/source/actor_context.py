"""Root-owned performer's physical operation context and full consumer adapter."""
import hashlib
import os
import stat
from common import Refused, INPUT_MAX, DOCUMENT_MAX, exact, canonical, parse, sha, domain, mono
from custody import Held, OutputStore, identity9
from consumer_bridge import OrdinaryInput
from admission import ALL15


class ActorContext:
    def __init__(self, admission, actor_fact, meter, call, consumer, ordinary):
        self.admission,self.meter,self.call,self.consumer=admission,meter,call,consumer
        self.retained_installers=[]
        self.meter.retain_local_owner(self)
        self.sources=admission["inputs"]
        self.actor_id,self.selector_id=actor_fact["actor_id"],ordinary["selector_id"]
        self.stage_root=admission["image"]["stage_root"]
        self.store=OutputStore(admission["output_root"]+"/actor",meter)
        self.counter=0;self.raw_proofs={};self.literal_proofs={};self.tree_proofs={};self.transport_verified={}
        self.base_members=admission["image"]["base_members"]
        self.ordinary=OrdinaryInput(ordinary,self.sources,meter,consumer,self.actor_id)
        self.expected=self.ordinary.expected
        # No installed facts are consumed until this actor has actually
        # acquired/retained the complete class collection and Root selected it.
        recipe=consumer("recipe_planner")
        self.raw_held=recipe._bind_streams(self.ordinary.context,self.ordinary.streams,recipe._ConstructionMeter())
        self.composition=None;self.material_phase=True
        self.legacy_closures={}
        self.predecessors=[]

    def new_installer(self,name,selected,existing_directories=()):
        from extraction import Installer
        install=Installer.__new__(Installer)
        self.retained_installers.append(install)
        self.meter.retain_local_owner(install)
        install.__init__(self.stage_root,self.meter,name,selected,existing_directories)
        return install
    def close_installer(self,install):
        install.close()
        self.meter.publish_local_owners()

    def native(self,verb,inputs):
        return self.call({"type":"native","operation":self.operation,"verb":verb,"inputs":inputs})
    def begin(self,name,actual_input):
        self.operation=name
        return self.call({"type":"begin","name":name,"input_body":actual_input})["started_ns"]
    def failure(self,name,exc):
        from common import error_fact
        self.call({"type":"failure","name":name,"error":error_fact(exc,"execution")})
    def normalize(self,section,target,result):
        event=self.put_body("event",result)
        reply=self.call({"type":"normalize","section":section,"target":target,"event_pin":event["pin"]})
        with Held(reply["record"]["pin"]["path"],reply["record"]["pin"],self.meter,INPUT_MAX,True) as body:
            reply["body"]=parse(body.read(INPUT_MAX),self.meter)
        if section=="operations":
            self.ordinary.add_performing(reply)
            if reply["expected"] is None:
                raise Refused("independent_selection_after_Root_retention_required","postdelivery",
                    {"retained_observed_pin":reply["record"]["pin"],"target":target})
            full=self.consumer_operation_output(target,reply)
            reply["full_output_body"]=full["full_output_body"]
            reply["full_output_sha256"]=full["output_sha256"]
            self.predecessors.append(full)
        elif not self.material_phase and reply["expected"] is not None:self.ordinary.add_performing(reply)
        return reply

    def retain_material(self,source_id,event):
        source=self.sources[source_id]
        with Held(source["path"],source,self.meter,DOCUMENT_MAX) as body:
            value=dict(event)
            value["input_body"]={"document_kind":source["kind"],"path":source["logical_path"],
                "sha256":body.body_sha,"size":body.size}
            value["target"]=source["kind"]+"/"+source["logical_path"]
            value["started_ns"]=self.call({"type":"operation-window","name":self.operation})["started_ns"]
            value["finished_ns"]=mono()
            return self.normalize("materials",value["target"],value)

    def retain_closure(self,role,event):
        value=dict(event);wanted=self.expected["data_packages" if role=="data" else "interpreter_packages"]
        value["package_bindings"]=[r for r in event["package_bindings"] if r["name"] in wanted]
        if {r["name"] for r in value["package_bindings"]}!=set(wanted):raise Refused("complete_closure_packages")
        value["target"]=role
        value["input_body"]={"role":role,"data_packages":self.expected["data_packages"],
            "interpreter_packages":self.expected["interpreter_packages"]}
        value["started_ns"]=self.call({"type":"operation-window","name":self.operation})["started_ns"]
        value["finished_ns"]=mono()
        # The diagnostic five-field body has its own complete physical preimage
        # and SHA; it is never substituted for this eleven-field typed body.
        legacy={k:value[k] for k in ("members","dependencies","abi","resources","package_bindings")}
        legacy["custody"]={"actual_outer":self.independent_outer(),
            "raw_input_preimages":[dict(self.sources[r["source_id"]]) for r in self.admission["members"][self.operation]],
            "effects_granted":False}
        legacy.pop("package_bindings")
        record=self.put_body("legacy-"+role+"-closure",legacy)
        self.legacy_closures[role]=record["pin"]
        self.call({"type":"legacy-closure","role":role,"pin":record["pin"]})
        return self.normalize("closures",role,value)

    def select_full_material_phase(self):
        selected=self.call({"type":"phase-independent-ordinary"})
        ordinary=OrdinaryInput(selected["ordinary"],selected["sources"],self.meter,self.consumer,self.actor_id)
        self.ordinary=ordinary;self.expected=ordinary.expected
        self.sources=ordinary.sources
        self.composition=ordinary.composition();self.material_phase=False
        self.material_revision=self.current_material_revision()
        if self.composition["full_document_vector"]["all_materials_closed"] is not True:
            raise Refused("fresh_complete_material_composition")

    def refresh_composition(self):
        if self.material_phase:raise Refused("material_phase_not_selected")
        revision=self.current_material_revision()
        if revision!=self.material_revision:
            self.composition=self.ordinary.composition();self.material_revision=revision
        return self.composition

    def current_material_revision(self):
        context=self.ordinary.context
        return domain("friday.sol053.full-material-composition-revision.v1",{
            "expected":self.ordinary.expected,"presented":self.ordinary.presented,
            "contracts":context["document_contracts"],"receipts":context["document_receipts"],
            "literals":context["wheel_literal_contracts"],"legacy":context["closure_bodies"],
            "materials":context["performing_contracts"]["materials"],
            "closures":context["performing_contracts"]["closures"]},self.meter,33_554_432)

    def retain_final_domains(self,event):
        from snapshot_producer import observed_snapshot
        recipe=self.consumer("recipe_planner")
        manifest=recipe.final_manifest_projection(self.predecessors)
        snapshot=observed_snapshot(self,manifest,event)
        value=dict(event);value["target"]="a009"
        value["input_body"]={k:v for k,v in snapshot.items() if k!="snapshot_id"}
        value["started_ns"]=self.call({"type":"operation-window","name":self.operation})["started_ns"]
        value["finished_ns"]=mono()
        self.normalize("snapshot","a009",value)
        emitted=next(r for r in self.predecessors if r["name"]=="write-final-manifest")
        files=[r for r in event["members"] if r["path"] in emitted["full_output_body"]["performing_observation"]["body"]["output_paths"]]
        if len(files)!=1:raise Refused("emitted_manifest_physical_file")
        with Held(files[0]["physical_path"],self.pin(files[0]),self.meter,INPUT_MAX) as held:
            emitted_raw=held.read(INPUT_MAX)
        value["target"]="a009-custody";value["input_body"]={"inventory_manifest_sha256":manifest["manifest_sha256"],
            "emitted_manifest_file_sha256":sha(emitted_raw,self.meter)}
        return self.normalize("materials","a009-custody",value)

    def put_body(self,prefix,value):
        name=prefix+"-"+str(self.counter);self.counter+=1
        # Full parsed raw/normalized/armor bodies become physical preimages,
        # never omitted, stringified or represented by hashes without bodies.
        def physical_bytes(item,depth=0):
            if depth>24:raise Refused("event_depth")
            if type(item) is bytes:
                n="preimage-"+str(self.counter);self.counter+=1
                record=self.store.put(n,item,"member","actor/"+n)
                return {"physical_byte_preimage":record}
            if type(item) is dict:
                if len(item)>512:raise Refused("event_items")
                return {k:physical_bytes(v,depth+1) for k,v in item.items()}
            if type(item) is list:
                if len(item)>512:raise Refused("event_items")
                return [physical_bytes(v,depth+1) for v in item]
            return item
        return self.store.put(name,canonical(physical_bytes(value),self.meter),"member","actor/"+name)

    def selected(self,kind,path):
        found=[k for k,v in self.sources.items() if v["kind"]==kind and v["logical_path"]==path]
        if len(found)!=1:raise Refused("complete_source_selector")
        return found[0]
    def class_sources(self,kind):
        return [k for k,v in self.sources.items() if v["kind"]==kind]
    def pin(self,row):
        return {"path":row["physical_path"],"bytes":row["size"],"sha256":row["sha256"],
                "identity9_decimal_strings":row["identity9_decimal_strings"]}
    def physical(self,row):
        if row["kind"]=="regular":
            with Held(row["physical_path"],self.pin(row),self.meter,DOCUMENT_MAX):pass
        else:
            observed=identity9(os.lstat(row["physical_path"]))
            if row["kind"]=="directory" and observed[:5]==row["identity9_decimal_strings"][:5]:
                # Child creation changes nlink/size/mtime/ctime of this owned
                # directory. Preserve each already-retained historical receipt;
                # publish fresh actual metadata for the later complete closure.
                row["identity9_decimal_strings"]=observed
            elif observed!=row["identity9_decimal_strings"]:
                raise Refused("actual_physical_member")
    def verify_all_members(self,rows):
        if len(rows)>512:raise Refused("member_limit")
        for row in rows:self.physical(row)
        complete={r["path"]:r for r in self.base_members}
        for row in rows:
            previous=complete.get(row["path"])
            if previous is not None and previous!=row:raise Refused("duplicate_actual_base_member")
            complete[row["path"]]=row
        if len(complete)>512:raise Refused("member_limit")
        logical=[]
        for row in complete.values():
            s=os.lstat(row["physical_path"]);path=row["path"]
            logical.append({"path":path,"kind":row["kind"],"device":s.st_dev,"uid":s.st_uid,"gid":s.st_gid,
                "parent_path":"" if path in ("/opt/friday/quality-toolchain/venv","/work/candidate","/inputs/golden") else path.rsplit("/",1)[0] if "/" in path else ""})
        if self.consumer("whole_join").bind_member_hierarchy(logical)["closed"] is not True:
            raise Refused("full_actual_hierarchy")

    def actual_abi(self,members,elf_facts):
        abi=self.admission["image"]["abi"]
        exact(abi,("architecture","python_abi","loader_path","loader_sha256","libraries"),"actual_abi")
        if abi["architecture"]!="amd64" or abi["python_abi"]!="cp314-regular":raise Refused("python_abi")
        by={r["path"]:r for r in members}
        loader=by.get(abi["loader_path"])
        if loader is None or loader["sha256"]!=abi["loader_sha256"] or abi["loader_path"] not in elf_facts:
            raise Refused("actual_loader")
        if any(path not in by or path not in elf_facts for path in abi["libraries"]):
            raise Refused("actual_native_libraries")
        return dict(abi)

    def actual_resources(self,members):
        by={r["path"]:r for r in members}
        resources=[]
        for selected in self.admission["image"]["resources"]:
            row=by.get(selected["path"])
            if row is None or row["kind"]!="regular" or row["sha256"]!=selected["sha256"] or row["size"]!=selected["size"]:
                raise Refused("actual_resource_body")
            self.physical(row);resources.append(dict(selected))
        if len({r["role"] for r in resources})!=len(resources):raise Refused("resource_roles")
        return resources

    def actual_packages(self,members):
        paths={r["path"] for r in members}
        out=[]
        for package in self.admission["image"]["package_bindings"]:
            exact(package,("name","members"),"package_binding")
            selected=[p for p in package["members"] if p in paths]
            if selected:
                if selected!=package["members"]:raise Refused("complete_package_members")
                out.append({"name":package["name"],"members":selected})
        if len(out)>350:raise Refused("artifact_limit")
        return out

    def exact_version(self,record,wanted):
        if record["exit_code"]!=0:raise Refused("native_version","execution")
        with Held(record["stdout"]["pin"]["path"],record["stdout"]["pin"],self.meter,INPUT_MAX,True) as out:
            if out.read(INPUT_MAX)!=wanted.encode("ascii"):raise Refused("native_version")

    def literal_wheel(self,literal):
        return {"name":literal["name"],"version":literal["version"],
            "requires_python":None if literal["requires_python"] in (None,"") else literal["requires_python"],
            "tags":[literal["python_tag"]+"-"+literal["abi_tag"]+"-"+literal["platform_tag"]]}

    def publish_raw_auth(self,proofs):
        # Retain complete producer bodies; independently selected contracts and
        # expected capabilities are preserved rather than assigned from output.
        self.raw_proofs.update(proofs)
        record=self.put_body("all13-raw-auth",proofs)
        self.call({"type":"authentication","operation":self.operation,"record_pin":record["pin"]})
    def publish_node_auth(self,proof,selection):
        record=self.put_body("node-raw-auth",{"proof":proof,"selection":selection})
        self.call({"type":"authentication","operation":self.operation,"record_pin":record["pin"]})
    def publish_wheel_literal(self,literal,metadata,proof):
        if literal["filename"] in self.literal_proofs:raise Refused("duplicate_wheel")
        self.literal_proofs[literal["filename"]]={"literal":literal,"metadata":metadata,"proof":proof}
        record=self.put_body("wheel-literal",self.literal_proofs[literal["filename"]])
        self.call({"type":"authentication","operation":self.operation,"record_pin":record["pin"]})

    def verify_browser_resources(self,members,expected):
        files={r["source_name"] for r in members if r["kind"]=="regular"}
        required=self.admission["image"]["browser_required_members"]
        if set(required)!=files:raise Refused("complete_browser_resources")
        if len(expected["archives"])!=3:raise Refused("complete_browser_archives")

    def verify_data_packages(self,members,required):
        actual={p["name"] for p in self.actual_packages(self.base_members+members)}
        if actual!=set(required):raise Refused("complete_data_packages")

    def verify_tree(self,kind,identity):
        selected=self.admission["image"]["trees"][kind]
        if selected["commit"]!=identity["commit"] or kind=="candidate" and selected["tree"]!=identity["tree"]:
            raise Refused("tree_identity")
        # Actual complete Git object preimages, read-only; no Git command/index
        # mutation, fetch, checkout or archive-derived identity substitution.
        for oid,source_id in selected["objects"].items():
            pin=self.sources[source_id]
            with Held(pin["path"],pin,self.meter,DOCUMENT_MAX) as held:
                raw=held.read(DOCUMENT_MAX)
                object_type=pin["git_object_type"]
                if object_type not in ("commit","tree","blob"):raise Refused("git_object_type")
                preimage=object_type.encode()+b" "+str(len(raw)).encode()+b"\0"+raw
                git_hold=self.meter.reserve("git-object-sha1", hash_bytes=len(preimage), allocation=64)
                try:
                    git_hold.commit(hash_bytes=len(preimage))
                    computed=hashlib.sha1(preimage).hexdigest()
                finally:
                    git_hold.release()
                if computed!=oid:raise Refused("full_git_object")
                if oid==selected["commit"]:
                    first=raw.split(b"\n",1)[0]
                    if first!=b"tree "+selected["tree"].encode():raise Refused("commit_tree")
        if selected["commit"] not in selected["objects"] or selected["tree"] not in selected["objects"]:
            raise Refused("complete_git_objects")
        # Exact tree rows are independently selected, and every source file/blob
        # has a reached physical body. Directory object closure is verified below.
        active=set();reached=set();filepaths=set()
        def walk(oid,prefix,depth=0):
            if depth>24 or oid in active or len(reached)>512:raise Refused("git_tree_capacity")
            active.add(oid);reached.add(oid)
            if oid not in selected["objects"]:raise Refused("tree_missing")
            source=self.sources[selected["objects"][oid]]
            with Held(source["path"],source,self.meter,DOCUMENT_MAX) as held:raw=held.read(DOCUMENT_MAX)
            at=0;seen=set()
            while at<len(raw):
                zero=raw.find(b"\0",at)
                if zero<0 or zero+21>len(raw):raise Refused("git_tree")
                header=raw[at:zero];mode,name=header.split(b" ",1)
                label=name.decode("utf-8")
                if "/" in label or label in ("",".","..") or label in seen:raise Refused("git_tree_name")
                seen.add(label);child=raw[zero+1:zero+21].hex();at=zero+21
                path=prefix+label
                if mode in (b"40000",b"040000"):walk(child,path+"/",depth+1)
                elif mode in (b"100644",b"100755",b"120000"):
                    if selected["files"].get(path)!=child:raise Refused("full_tree_file")
                    if child not in selected["objects"]:raise Refused("full_tree_blob")
                    filepaths.add(path);reached.add(child)
                else:raise Refused("git_tree_mode")
            active.remove(oid)
        walk(selected["tree"],"")
        if filepaths!=set(selected["files"]):raise Refused("complete_git_file_set")
        if reached|{selected["commit"]}!=set(selected["objects"]):raise Refused("complete_git_object_set")
        if set(selected["files"])!={r["tree_path"] for r in self.admission["members"]["bind-"+kind] if r["kind"]!="directory"}:
            raise Refused("full_tree_inventory")
        self.tree_proofs[kind]=selected

    def generated_member(self,name,raw):
        plan=[r for r in self.admission["members"][name] if r["generated"] is True]
        if len(plan)!=1:raise Refused("generated_member_plan")
        row=plan[0]
        if row["sha256"]!=sha(raw,self.meter) or row["size"]!=len(raw):
            if len(raw)>row["size"]:raise Refused("generated_declared_capacity")
            preimage=self.store.put("generated-preimage-"+str(self.counter),raw,"member");self.counter+=1
            row=self.call({"type":"independently-selected-generated-member","operation":name,
                "public_preimage_pin":preimage["pin"]})["plan"]
        from extraction import Installer
        install=self.new_installer(name,[row])
        try:
            install.consume(row["source_name"],"regular",len(raw),__import__("io").BytesIO(raw),
                            executable=row["mode"]=="0555")
            sealed=install.seal()
            return sealed
        finally:
            self.close_installer(install)

    def consumer_operation_output(self,name,receipt):
        recipe=self.consumer("recipe_planner");body=receipt["body"]
        spec=next(r for r in self.expected["operations"] if r["name"]==name)
        inventory=[recipe._member_from_fact({k:v for k,v in r.items() if k!="source_ref"}) for r in body["members"]]
        if self.consumer("whole_join").bind_member_hierarchy(inventory)["closed"] is not True:
            raise Refused("operation_computation")
        owned=set(body["output_paths"])
        if any(r["operation"]!=name for r in body["members"] if r["path"] in owned):raise Refused("operation_computation")
        members=[m for m in inventory if m["path"] in owned]
        if not members or len(members)>spec["member_limit"]:raise Refused("member_limit")
        predecessors=[r for r in self.predecessors if r["name"] in spec["dependencies"]]
        emitted=None
        if name in ("assemble-members","write-final-manifest"):
            rows=[dict(m) for r in self.predecessors for m in r["members"]]
            blob=self.consumer("canonical").canonical_bytes(rows)
            path="var/lib/rootfs/members" if name=="assemble-members" else "var/lib/rootfs/manifest/manifest"
            selected_files=[m for m in members if m["path"]==path]
            if len(selected_files)!=1 or selected_files[0]["content_sha256"]!=sha(blob,self.meter) or selected_files[0]["size"]!=len(blob):raise Refused("operation_computation")
            if name=="write-final-manifest":emitted=blob.decode("ascii")
        predecessor_closed=all(r["status"]=="STRUCTURALLY_BOUND" for r in predecessors)
        inherited=domain("friday.sol037.full-predecessor-closure.v1",
            [{"name":r["name"],"output_sha256":r["output_sha256"]} for r in predecessors],self.meter)
        for member in members:
            member["input_sha256"]=inherited
            if not predecessor_closed:member["status"]="NOT_PROVEN"
        prior=[m for r in self.predecessors for m in r["members"]]
        if len(prior)+len(members)>512 or any(m["path"] in {r["path"] for r in prior} for m in members):raise Refused("duplicate_member")
        # Use the actual full unchanged runtime consumer, with reached complete
        # physical pages, before deriving the full public operation output hash.
        pages,sequence=self.call({"type":"page-catalog"})["catalog"]
        context=dict(self.ordinary.context);context["page_sequence"]=sequence
        source=self.consumer("document_windows").FilePageSource(pages)
        held=recipe._bind_streams(context,source,recipe._ConstructionMeter())
        joined=self.consumer("performing_contracts").consume(context,held,"operations",name,body["input_body"])
        if joined is None or joined["body"]!=body:raise Refused("fresh_full_operation_consumer")
        full_body={"name":name,"destination":spec["destination"],"dependencies":spec["dependencies"],
            "effects_available":False,"members":members,"publisher_proof":False,
            "specific_inputs":body["input_body"],"performing_observation":joined,"fixed_fallback":False}
        return {"name":name,"members":members,"destination":spec["destination"],
            "effects_available":False,"publisher_proof":False,"status":"STRUCTURALLY_BOUND" if predecessor_closed and all(m["status"]=="STRUCTURALLY_BOUND" for m in members) else "NOT_PROVEN",
            "full_output_body":full_body,"output_sha256":domain("friday.sol037.full-operation-output.v1",full_body,self.meter),
            "performing_ref":joined["observed_body_ref"],"emitted_body_canonical":emitted}

    def consumer_member_projection(self,completed):
        # The exact planner rows (including inherited input hash and flags) are
        # retained by reached A128 runtime/full-operation consumers above.
        return [dict(m) for r in self.predecessors for m in r["members"]]

    def verify_a009_final_domains(self,members):
        candidate=[r for r in members if r["path"].startswith("/work/candidate/")]
        expected=self.admission["coverage"]["a009_candidate7_top11"]
        if len(candidate)!=7 or len(expected["top_level_paths"])!=11:
            raise Refused("a009_full_domains")
        self.verify_all_members(members)

    def independent_outer(self):
        return self.call({"type":"outer"})["observation"]

    def close(self):
        from common import error_fact
        for install in self.retained_installers:
            try:
                if hasattr(install,"fdjournal"):self.close_installer(install)
            except BaseException as exc:self.meter.note_cleanup(error_fact(exc,"terminal"))
        try:self.store.close()
        except BaseException as exc:self.meter.note_cleanup(error_fact(exc,"terminal"))
        try:self.meter.publish_local_owners()
        except BaseException as exc:self.meter.note_cleanup(error_fact(exc,"terminal"))
