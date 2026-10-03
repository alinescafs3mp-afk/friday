"""Future entry called IN the independently enrolled EXISTING Root native tool.

This module is not an issuer/install action and is not invoked in A138. A Root
owner first qualifies a new exact admission after independent Source review.
Only this actual Root parent creates the new launcher, pipes, pidfd, gates,
native children, observer, physical receipt pages and final retained terminal.
"""
import contextlib
import errno
import hashlib
import json
import os
import selectors
import signal
import stat
import struct
import fcntl
from common import (Refused, INPUT_MAX, DOCUMENT_MAX, OUTPUT_MAX, WORKERS_MAX,
                    exact, parse, canonical, domain, sha, mono, error_fact,
                    bounded_refusal, bounded_tree_paths, PreObserverHash, Frame,
                    decode_history_rows,history_domain_books,history_book_rows,
                    validate_history_collection)
from custody import Held, OutputStore, PreparedFullBody, open_absolute, identity9
from observer import (RootObserver, proc_start, FinalArena, validate_full_value_arena,
    bind_selected_preimages,full_value_history_domain)
from admission import protected_enrollment, actual_root_tool, qualify, full_snapshot, ALL15
from native import RootNative, child_guard,actor_sandbox
from normalization import RootNormalizer
from consumer_bridge import HeldConsumerLoader, OrdinaryInput, require_all69_and_two_positives
from lifetime import (OwnedFDs,bounded_direct_reap,ForkOwner,receive_fork_owner,
    stock_inherited_upper)

# Existing Root process owns these references. No daemon, model, hidden service
# or cleanup loop is created. A handoff must be durably pinned before return.
RETAINED_ROOT_OWNERS={}


class RootToolAdapter:
    def __init__(self):
        self.entry_ns=mono()
        self.partial_owner_pid=os.getpid()
        RETAINED_ROOT_OWNERS[(self.partial_owner_pid,self.entry_ns)]=self
        self.owner_keys={(self.partial_owner_pid,self.entry_ns)}
        self.final_arena=FinalArena(self)
        self.validation_holds=[];self.retained_journals=[]
        self.prefix_error_arenas=[]
        self.actor_local_graph=None;self.actor_local_sequence=0
        # Ownership exists before the first fallible read/effect. A constructor
        # failure must never lose the admission helper or pretend effect-free.
        self.enrollment=self.enrollment_pin=self.observer=self.root_fact=None
        self.store=self.native=self.loader=self.admission=self.qualification=None
        self.fdjournal=OwnedFDs();self.finished=None;self.prepared=False
        self.actor_pid=self.actor_pidfd=-1;self.actor_started_ns=0
        self.actor_id=None;self.actor_uid=self.actor_gid=None
        self.actor_env={"LC_ALL":"C","LANG":"C"}
        self.operation_windows={};self.current_operation=None
        self.authentication={};self.normalizer=RootNormalizer(self)
        self.page_files=[];self.page_sequence=[];self.packed={}
        self.counter=0;self.errors=[];self.case=None;self.capture_out=bytearray();self.capture_err=bytearray()
        self.actor_status=None;self.actor_usage=None;self.frame=None
        self.actual_launch_fact=None;self.capture_fds={};self.retained_receipts=[];self.selector_receipts=[]
        self.cleanup_attempted=False;self.actor_exe_fact=None;self.bundle_hold=None;self.actor_graph_hold=None
        self.scope_cleanup_attempted=False
        self.material_phase=False;self.legacy_closures={};self.generated_plans={}
        self.phase_ordinary=None;self.final_binding=None;self.full_output_pin=None
        self.final_expected_output=None;self.final_public_pin=None
        self.actor_exec_leases=[];self.owner_closed=False;self.forward_fd=self.tail_fd=-1
        self.delivery_retire_attempted=False;self.delivery_pins=None
        self.stream_cache={}
        self.owner_export_hold=None;self.actor_fork_owner=None;self.actor_fork_graph=None;self.actor_fork_hold=None
        self.actor_ownership_complete=False;self.bootstrap_prefix_graph=None
        self.prefix_decoded_bundle=None;self.prefix_consumed_launch=None;self.prefix_foreign_unwalked=()
        self.sealed_bundle=None;self.sealed_bundle_sha256=None

    def prepare(self):
        from existing_root_caller import current_native_owner
        caller=current_native_owner()
        self.pre_hash=PreObserverHash.__new__(PreObserverHash)
        caller.retain_before_birth(self.pre_hash,'pre-observer-hash')
        self.pre_hash.__init__()
        self.enrollment,self.enrollment_pin=protected_enrollment(self.pre_hash)
        bind_selected_preimages(tuple(self.enrollment["source_files"])+tuple(self.enrollment["consumer_files"]))
        self.observer=RootObserver.__new__(RootObserver)
        caller.retain_before_birth(self.observer,'actual-partial-RootObserver')
        self.observer.__init__(self.enrollment["cgroup"],None,self.enrollment["root_namespace"]+":outer",self.entry_ns,carried_hash=self.pre_hash.hash_bytes,preowner=self.pre_hash)
        self.observer.retain_local_owner(self.fdjournal)
        self.root_fact=actual_root_tool(self.enrollment,self.observer)
        self.store=OutputStore.__new__(OutputStore)
        caller.retain_before_birth(self.store,'actual-partial-OutputStore')
        self.store.__init__(self.enrollment["output_root"],self.observer)
        # Real complete endpoints/readers precede admission helper/fork effects.
        # Existing terminal credit remains charged; no fresh cleanup reserve.
        self.observer.result_body_carrier=self.store.prepare_full_body('whole-result-values',self.observer.terminal_hold)
        self.final_body_carrier=self.store.prepare_full_body('whole-final-values',self.observer.terminal_hold)
        self.final_document_carrier=self.store.prepare_full_body('whole-final-value-document',self.observer.terminal_hold)
        self.final_document_carrier.extra_read_passes=1;self.final_document_carrier.extra_hash_passes=1
        # Existing enrolled Root identity and observer precede these endpoints.
        # Retain a full preparation-error body BEFORE the large snapshot walk.
        # Snapshot still precedes every signature/helper/performer effect.
        # Actual native entry retained every partial instance before construction.
        # Pre-carrier errors use its preowned full memory bank, not a late open.
        self.snapshot=full_snapshot(self.enrollment,self.observer)
        for name in ("whole-actor-stdout","whole-actor-stderr"):self.store.prepare_reserved(name)
        # The durable forward endpoint and its slot exist before the first
        # native helper, and remain a named explicit delivery-owner lease.
        self.forward_hold=self.observer.reserve("durable-terminal-and-cleanup-tail-FDs",allocation=131072,slots=2)
        for name,attribute in (("whole-terminal","forward_fd"),("whole-cleanup-tail","tail_fd")):
            setattr(self,attribute,self.fdjournal.acquire(os.open,name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=self.store.root_fd,credit=self.forward_hold,holder="delivery-"+name))
        self.native=RootNative(self.observer,self.store,self.enrollment)
        self.admission,self.qualification=qualify(self.enrollment,self.root_fact,self.observer,self.native)
        self.native.qualify(self.admission)
        caller.bind_source(self)
        self.material_phase=self.admission["launch_mode"]=="perform-and-retain"
        self.loader=HeldConsumerLoader(self.enrollment["consumer_files"],self.observer)
        self.loader.acquire().enter()
        self.consumer=self.loader.module
        require_all69_and_two_positives(self.admission,self.consumer)
        self.validate_image()
        self.prepare_private_paths()
        self.prepared=True
        return self

    def _close_temp(self, fd):
        self.fdjournal.close_one(fd)
        if fd in self.fdjournal.fds:
            meta=self.fdjournal.meta.get(fd, {})
            fact={"cause":"FD_CLOSE_UNCONFIRMED","fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings"),"status":meta.get("status")}
            if self.observer is not None:self.observer.note_cleanup(fact)
            else:self.errors.append(fact)

    def prepare_private_paths(self):
        # Names are Root-selected after qualification; the Root FD, not a child
        # pathname request, is the preparation boundary. Existing dirty trees
        # are rejected; no deletion/reuse of a different generation.
        if self.admission["image"]["stage_root"]!=self.store.root+"/stage":raise Refused("stage_root")
        hold=self.observer.reserve("prepared-path-FD-before-effect",allocation=65536,slots=1)
        self.fdjournal.grant(hold)
        try:
            for name in ("actor","stage"):
                os.mkdir(name,0o700,dir_fd=self.store.root_fd)
                fd=self.fdjournal.acquire(os.open,name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=self.store.root_fd,holder="prepared-path",credit=hold)
                try:
                    s=os.fstat(fd)
                    if stat.S_IMODE(s.st_mode)!=0o700 or s.st_uid!=os.getuid():raise Refused("prepared_path_custody")
                    self.observer.note_partial(self.store.root+"/"+name,0)
                finally:self._close_temp(fd)
        finally:
            if not any(self.fdjournal.meta[f]["credit"]==hold.token for f in self.fdjournal.fds):hold.release()
    def validate_image(self):
        from capacity import validate_complete_capacity, VALIDATION_READ, VALIDATION_ALLOC
        image_hold=self.observer.reserve("root-image-validation-before-read",
            reads=VALIDATION_READ, allocation=VALIDATION_ALLOC, slots=4, hash_bytes=0)
        self.validation_holds.append(image_hold)
        self.fdjournal.grant(image_hold)
        image_hold.commit(reads=VALIDATION_READ)
        try:
            image=self.admission["image"]
            rootfd=open_absolute(image["path"],os.O_RDONLY|os.O_DIRECTORY,journal=self.fdjournal)
            try:
                if identity9(os.fstat(rootfd))!=image["root_identity9"]:raise Refused("image_root_identity")
            finally:
                self._close_temp(rootfd)
            mountinfo=open_absolute("/proc/self/mountinfo",journal=self.fdjournal)
            try:raw=os.read(mountinfo,2_000_001)
            finally:
                self._close_temp(mountinfo)
            if len(raw)>2_000_000:raise Refused("image_mountinfo")
            matches=[line for line in raw.decode().splitlines() if line.split()[4]==image["path"]]
            if len(matches)!=1 or "ro" not in matches[0].split()[5].split(","):
                raise Refused("image_not_independently_read_only")
            if len(image["members"])>512:raise Refused("full_image_members")
            virtual=image["launcher_virtual_path"]
            if not virtual.startswith("/") or any(p in ("",".","..") for p in virtual.split("/")[1:]):raise Refused("launcher_virtual_path")
            if image["launcher"]["path"]!=image["path"]+virtual:raise Refused("launcher_image_path")
            if any(not p["path"].startswith(image["path"]+"/") for p in image["launcher_dependencies"]):raise Refused("launcher_dependency_image_path")
            expected=set()
            for pin in image["members"]:
                if not pin["path"].startswith(image["path"]+"/"):raise Refused("image_path")
                expected.add(pin["path"])
                if pin["kind"]=="regular":
                    with Held(pin["path"],pin,self.observer,DOCUMENT_MAX):pass
                else:
                    if identity9(os.lstat(pin["path"]))!=pin["identity9_decimal_strings"]:
                        raise Refused("image_member_identity")
            actual=set(bounded_tree_paths(image["path"], 512))
            if actual!=expected:raise Refused("complete_image_inventory")
            if len(self.admission["inputs"])>512:raise Refused("physical_input_registry_capacity")
            for pin in self.admission["inputs"].values():
                with Held(pin["path"],pin,self.observer,DOCUMENT_MAX):pass
            for spec in self.admission["tools"].values():
                with Held(spec["pin"]["path"],spec["pin"],self.observer,DOCUMENT_MAX):pass
                if not spec["dependencies"]:raise Refused("tool_dependency_inventory")
                for pin in spec["dependencies"]:
                    with Held(pin["path"],pin,self.observer,DOCUMENT_MAX):pass
            # The actor sees only the selected frozen roots inside its private
            # image. These must contain every Source/consumer/input pathname.
            roots=image["actor_readonly_roots"]
            source_paths=[r["path"] for r in self.enrollment["source_files"]+self.enrollment["consumer_files"]]
            source_paths += [r["path"] for r in self.admission["inputs"].values()]
            if len(roots)>32 or any(not any(p.startswith(r["path"]+"/") for r in roots) for p in source_paths):raise Refused("complete_actor_readonly_roots")
            for root in roots:
                fd=open_absolute(root["path"],os.O_RDONLY|os.O_DIRECTORY,journal=self.fdjournal)
                try:
                    if identity9(os.fstat(fd))!=root["identity9_decimal_strings"]:raise Refused("actor_readonly_root_identity")
                finally:
                    self._close_temp(fd)
                if root["path"]==self.store.root or self.store.root.startswith(root["path"]+"/"):raise Refused("actor_output_readonly_overlap")
            # No actor/native effect starts with a known invalid capacity declaration.
            total=sum(r["size"] for rows in self.admission["members"].values() for r in rows if r["kind"]=="regular")
            count=sum(len(rows) for rows in self.admission["members"].values())+len(image["base_members"])
            if total>OUTPUT_MAX or count>512:raise Refused("complete_declared_capacity")
            paths=[r["logical_path"] for rows in self.admission["members"].values() for r in rows]
            if len(paths)!=len(set(paths)):raise Refused("duplicate_operation_output_before_effect")
            # Complete historical process accounting stays in ABI128. Existing
            # independently signed full HTTPS bundles are the compatible transport
            # route; every record/body is still separately consumed in full.
            signatures=set()
            for pin in self.admission["inputs"].values():
                auth=pin.get("authentication")
                if type(auth) is dict and auth.get("kind") in ("independent-root-retained-https","independent-root-retained-https-bundle"):
                    signatures.add((auth["receipt_id"],auth["signature_id"],auth["key_id"]))
            generated=sum(r["generated"] is True for rows in self.admission["members"].values() for r in rows)
            upper=len(self.observer.processes)+1+1+14+4+1+15+2+1+generated+len(signatures)
            if upper>128:raise Refused("complete_history_requires_full_signed_transport_bundles_before_effect")
            self.native_process_upper=upper
            self.capacity=validate_complete_capacity(self.admission,self.enrollment,self.observer,total)
        finally:
            raw=None;matches=None;actual=None;expected=None;signatures=None;source_paths=None;roots=None;paths=None
            # Keep this arena through BOTH normal caller aliases and every
            # nested refusal traceback. Same-Root delivery retirement drops
            # the complete graph before releasing the actual credit.
    def check_owned_path(self,path):
        roots=(self.admission["output_root"],self.admission["image"]["stage_root"],self.admission["image"]["path"])
        if not any(path.startswith(r+"/") for r in roots):raise Refused("owned_physical_path")
        if any(v in ("",".","..") for v in path.split("/")[1:]):raise Refused("owned_physical_path")

    def add_page(self,pin,kind,path):
        if len(self.page_files)>=512:raise Refused("page_capacity")
        key=(kind,path,0)
        for row in self.page_files:
            if (row["kind"],row["path"],row["page_index"])==key:
                if row["sha256"]!=pin["sha256"] or row["identity9"]!=pin["identity9_decimal_strings"]:
                    raise Refused("page_identity")
                return
        self.page_files.append({"kind":kind,"path":path,"page_index":0,"file_path":pin["path"],
            "identity9":pin["identity9_decimal_strings"],"sha256":pin["sha256"]})
        self.page_sequence.append({"kind":kind,"path":path,"page_index":0,"page_count":1,
            "page_size":pin["bytes"],"sha256":pin["sha256"],"size":pin["bytes"],
            "body_sha256":pin["sha256"],"custody_sha256":None})

    def pack_preimage(self,held,kind,logical_path=None):
        """Closed regular packed pages; all raw bytes retained, no aliased hash."""
        key=(kind,held.path,held.body_sha,held.size,logical_path)
        if key in self.packed:return dict(self.packed[key])
        # The pack unit is the complete same-held file. Larger batching can only
        # occur in independent preparation with exact slice refs; never by
        # dropping original bodies or changing the 512-page bound.
        name="preimage-"+str(self.counter);self.counter+=1
        logical=held.path if logical_path is None else logical_path
        if len(logical)>240:logical=name
        record=self.store.put(name,held.read(DOCUMENT_MAX),kind,logical)
        self.add_page(record["pin"],kind,logical)
        ref=dict(record["ref"])
        self.packed[key]=ref
        return ref

    def pack_member_batch(self,facts):
        """Full byte ranges, not representative members or aliased hashes.

        Existing full copies are reused. New regular member preimages share one
        closed physical page with exact complete-document/slice SHA and size.
        """
        pins=[];seen=set();total=0
        for fact in facts:
            if fact["kind"]!="regular":continue
            key=("member",fact["physical_path"],fact["sha256"],fact["size"],None)
            if key in self.packed or key in seen:continue
            seen.add(key);total+=fact["size"]
            if total>OUTPUT_MAX:raise Refused("full_preimage_pack_capacity")
            pins.append((key,{"path":fact["physical_path"],"sha256":fact["sha256"],"bytes":fact["size"],
                "identity9_decimal_strings":fact["identity9_decimal_strings"]}))
        if not pins:return
        hold=self.observer.reserve("full-member-pack-before-allocation",reads=total,allocation=total*3+65536)
        try:
            body=bytearray(total);offset=0;rows=[]
            for key,pin in pins:
                with Held(pin["path"],pin,self.observer,DOCUMENT_MAX) as held:
                    raw=held.read(DOCUMENT_MAX);body[offset:offset+len(raw)]=raw
                rows.append((key,offset,len(raw)));offset+=len(raw);hold.commit(reads=len(raw))
            name="full-member-pack-"+str(self.counter);self.counter+=1
            record=self.store.put(name,bytes(body),"member",name)
            self.add_page(record["pin"],"member",name)
            for key,offset,size in rows:
                self.packed[key]={"kind":"member","path":name,"sha256":key[2],
                    "document_sha256":record["pin"]["sha256"],"offset":offset,"size":size}
        finally:hold.release()

    def actual_tool(self,abi):
        selected=self.admission["image"]["launcher"]
        with Held(selected["path"],selected,self.observer,DOCUMENT_MAX) as executable:
            ref=self.pack_preimage(executable,"native",self.admission["image"]["launcher_virtual_path"])
            out={"producer_id":self.actor_id,"executable":{"ref":ref,"size":executable.size},
                "dependencies":[],"argv":list(self.actor_argv),"abi":abi}
            if out["argv"][0]!=ref["path"]:raise Refused("actual_actor_argv")
        for pin in self.admission["image"]["launcher_dependencies"]:
            with Held(pin["path"],pin,self.observer,DOCUMENT_MAX) as held:
                virtual=pin["path"][len(self.admission["image"]["path"]):]
                if not virtual.startswith("/"):raise Refused("launcher_dependency_image_path")
                ref=self.pack_preimage(held,"native",virtual)
                out["dependencies"].append({"ref":ref,"size":held.size,"abi":"cp314-regular",
                    "custody":{"producer_id":self.actor_id,"path":ref["path"],"sha256":ref["sha256"],
                        "size":held.size,"effects_granted":False}})
        if not out["dependencies"]:raise Refused("launcher_dependency_inventory")
        return out

    def pack_material_records(self):
        receipts=[r for r in self.retained_receipts if r["section"] in ("materials","closures")]
        total=sum(r["record"]["pin"]["bytes"] for r in receipts)
        limit=self.capacity["declared"]["output_components"]["typed_pack_copy"]
        if total>limit:raise Refused("full_typed_batch_declared_capacity")
        hold=self.observer.reserve("full-typed-pack-before-allocation",allocation=total*3+65536)
        try:
            body=bytearray(total);offset=0;positions=[]
            for receipt in receipts:
                pin=receipt["record"]["pin"]
                with Held(pin["path"],pin,self.observer,INPUT_MAX,True) as held:
                    raw=held.read(INPUT_MAX);body[offset:offset+len(raw)]=raw
                positions.append((receipt,offset,len(raw)));offset+=len(raw)
            record=self.store.put("complete-typed-material-pack",bytes(body),"member")
            self.add_page(record["pin"],"member",record["ref"]["path"])
            for receipt,at,size in positions:
                receipt["record"]["ref"]={"kind":"member","path":record["ref"]["path"],
                    "sha256":receipt["record"]["pin"]["sha256"],"document_sha256":record["pin"]["sha256"],
                    "offset":at,"size":size}
        finally:hold.release()

    def actual_kernel(self):
        selected=self.admission["image"]["kernel"]
        with Held(selected["path"],selected,self.observer,DOCUMENT_MAX) as held:
            ref=self.pack_preimage(held,"kernel")
            return {"release":os.uname().release,"ref":ref,"size":held.size}

    def actual_operation_streams(self,target):
        window=self.operation_windows.get(target,{"stdout_at":0,"stderr_at":0})
        def store_stream(which,raw,at):
            size=len(raw)-at
            hold=self.observer.reserve("complete-actor-stream-copy-before-allocation",allocation=size*3+65536,reads=size)
            try:
                data=bytes(raw[at:]);key=(which,sha(data,self.observer),len(data));hold.commit(reads=len(data))
                rec=self.stream_cache.get(key)
                if rec is not None:
                    with Held(rec["pin"]["path"],rec["pin"],self.observer,INPUT_MAX,True) as same:
                        prior=same.read(INPUT_MAX)
                    if prior!=data:raise Refused("complete_stream_reuse_identity")
                    self.observer.retire_result(prior)
                else:
                    name="actor-"+which+"-"+str(self.counter);self.counter+=1
                    rec=self.store.put(name,data,"native",name);self.stream_cache[key]=rec
                    self.add_page(rec["pin"],"native",name)
                return {"ref":rec["ref"],"size":len(data)}
            finally:hold.release()
        return store_stream("stdout",self.capture_out,window["stdout_at"]),store_stream("stderr",self.capture_err,window["stderr_at"])

    def native_receipts_for(self,target):
        begin=self.operation_windows.get(target,{"started_ns":self.actor_started_ns})["started_ns"]
        return [r for r in self.native.invocations if r["started_ns"]>=begin]

    def input_preimages(self,target):
        return [dict(pin) for pin in self.admission["inputs"].values() if target in pin["operations"]]

    def external_expected(self,section,target):
        selected=self.case["performing_expected"].get(section+"/"+target)
        if selected is None:raise Refused("independent_full_expected_body_required")
        pin=self.admission["inputs"][selected["source_id"]]
        if pin["producer_id"] in (self.actor_id,self.observer.observer_id):
            raise Refused("own_derived_expected_body")
        return {"pin":pin,"kind":pin["kind"],"logical_path":pin["logical_path"],"producer_id":pin["producer_id"]}

    def verify_semantic_event(self,section,target,event):
        exact(event,("members","output_paths","dependencies","abi","resources","package_bindings",
            "authentication","native_receipts","status","input_body","target","started_ns","finished_ns")
            if "publisher_gap" not in event else ("members","output_paths","dependencies","abi","resources","package_bindings",
            "authentication","native_receipts","status","input_body","target","started_ns","finished_ns","publisher_gap"),"performing_event")
        if event["target"]!=target:raise Refused("event_target")
        if section=="operations":
            if self.operation_windows[target]["input_body"]!=event["input_body"]:
                raise Refused("actual_input_preimage")
            plans=self.admission["members"][target]
            outputs={r["logical_path"] for r in plans}
            if set(event["output_paths"])!=outputs:raise Refused("complete_operation_outputs")
            owned={r["path"] for r in event["members"] if r["path"] in outputs}
            if owned!=outputs:raise Refused("complete_operation_inventory")
            for plan in plans:
                if plan["generated"] is not True:continue
                row=next(r for r in event["members"] if r["path"]==plan["logical_path"])
                if row["kind"]!="regular" or row["sha256"]!=plan["sha256"] or row["size"]!=plan["size"]:
                    raise Refused("complete_generated_plan_actual_bytes")
                self.generated_plans.setdefault(target,[])
                if plan not in self.generated_plans[target]:self.generated_plans[target].append(dict(plan))
        known={r["pid"]:r for r in self.native.invocations if "terminal" in r}
        for receipt in event["native_receipts"]:
            if receipt["pid"] not in known or receipt!=known[receipt["pid"]]["terminal"] or receipt["exit_code"]!=0:
                raise Refused("actual_native_receipt")
        if target in ("authenticate-ubuntu-indexes","authenticate-node-archive","authenticate-wheels","map-browser-resources") and not event["authentication"]:
            raise Refused("actual_class_authentication")

    def verify_domains(self,body,event):
        members={r["path"]:r for r in body["members"]}
        if len(body["package_bindings"])>350 or len(body["dependencies"])>512:raise Refused("domain_capacity")
        for edge in body["dependencies"]:
            exact(edge,("consumer_path","provider_path","provider_sha256","abi"),"dependency")
            if edge["consumer_path"] not in members or edge["provider_path"] not in members or members[edge["provider_path"]]["content_sha256"]!=edge["provider_sha256"]:
                raise Refused("actual_dependency")
        abi=body["abi"]
        exact(abi,("architecture","python_abi","loader_path","loader_sha256","libraries"),"abi")
        if abi["architecture"]!="amd64" or abi["python_abi"]!="cp314-regular" or abi["loader_path"] not in members or members[abi["loader_path"]]["content_sha256"]!=abi["loader_sha256"]:
            raise Refused("actual_abi")
        for r in body["resources"]:
            exact(r,("role","path","sha256","size"),"resource")
            if r["path"] not in members or members[r["path"]]["content_sha256"]!=r["sha256"] or members[r["path"]]["size"]!=r["size"]:
                raise Refused("actual_resource")
        for package in body["package_bindings"]:
            exact(package,("name","members"),"package")
            if not package["members"] or any(p not in members for p in package["members"]):raise Refused("actual_package")

    def selected_case(self,case_id):
        ordinary=self.admission["ordinary"]
        source_id=ordinary["controls"].get(case_id)
        if source_id is None:
            source_id=next((r["source_id"] for r in ordinary["positives"] if r["case_id"]==case_id),None)
        if source_id is None:raise Refused("ordinary_full_package_required")
        pin=self.admission["inputs"][source_id]
        with Held(pin["path"],pin,self.observer,INPUT_MAX,True) as held:
            chosen=parse(held.read(INPUT_MAX),self.observer)
        from roles import validate_ordinary
        validate_ordinary(chosen,self.enrollment,self.observer)
        signature=chosen["selection_signature"]
        exact(signature,("kind","signature_id","key_id"),"independent_selection_signature")
        if signature["kind"]!="detached-independent-Root-selection":raise Refused("independent_selection_signature")
        proof=self.native.perform("external-custody","retained-signature",[source_id,signature["signature_id"],signature["key_id"]])
        if proof["exit_code"]!=0:raise Refused("independent_selection_signature")
        if chosen["case_id"]!=case_id:raise Refused("ordinary_case")
        self.case=chosen;self.selector_id=chosen["selector_id"]
        for pin in chosen["page_files"]:
            physical={"path":pin["file_path"],"bytes":int(pin["identity9"][6]),"sha256":pin["sha256"],"identity9_decimal_strings":pin["identity9"]}
            self.add_page(physical,pin["kind"],pin["path"])
        return chosen

    def _validate_actor_completion(self,graph):
        def chunks(value,count,cause):
            if type(count) is not int or count<0 or type(value) is not list or len(value)>512:
                raise Refused(cause,'terminal')
            if any(type(part) is not list or len(part)>512 for part in value):raise Refused(cause,'terminal')
            rows=[r for part in value for r in part]
            if len(rows)!=count:raise Refused(cause,'terminal')
            return rows
        local=chunks(graph['local_chunks'],graph['local_count'],'actor_local_count')
        held=chunks(graph['held_chunks'],graph['held_count'],'actor_held_count')
        results=exact(graph['results'],('count','chunks','truncated'),'actor_result_body')
        if results['truncated'] is not False:raise Refused('actor_result_body','terminal')
        rows=chunks(results['chunks'],results['count'],'actor_result_count')
        arena=graph['value_arena']
        values=validate_full_value_arena(arena,self.actor_body_carrier)
        joined=graph['local_root_indexes']+graph['held_root_indexes']+graph['result_root_indexes']+graph['error_root_indexes']
        if joined!=arena['roots'] or graph['local_root_indexes']!=arena['roots'][:len(local)] or graph['held_root_indexes']!=arena['roots'][len(local):len(local)+len(held)]:
            raise Refused('actor_value_root_partition','terminal')
        if graph['result_root_indexes']!=arena['roots'][len(local)+len(held):len(local)+len(held)+len(rows)]:
            raise Refused('actor_result_body_count','terminal')
        pins={}
        if self.enrollment is not None:
            for pin in tuple(self.enrollment.get('source_files',()))+tuple(self.enrollment.get('consumer_files',())):
                path=pin.get('path')
                if path in pins:raise Refused('qualified_preimage_ambiguous','terminal')
                pins[path]=pin
        for kind,body in values:
            if kind=='qualified-function':
                code_kind,code_body=values[body['code']]
                if code_kind!='code-body' or values[code_body['co_filename']]!=['str',body['filename']]:
                    raise Refused('qualified_actual_code_body','terminal')
                pin=pins.get(body.get('filename'))
                if pin is None or body.get('preimage_sha256')!=pin.get('sha256') or body.get('preimage_bytes')!=pin.get('bytes'):
                    raise Refused('qualified_function_preimage','terminal')
                link=body.get('module_body')
                if type(link) is not int or not 0<=link<len(values):
                    raise Refused('qualified_function_globals','terminal')
                mod_kind,mod_body=values[link]
                if mod_kind!='qualified-module' or mod_body.get('module')!=body.get('module') or mod_body.get('file')!=body.get('filename'):
                    raise Refused('qualified_function_globals','terminal')
                if mod_body.get('preimage_sha256')!=pin.get('sha256') or mod_body.get('preimage_bytes')!=pin.get('bytes'):
                    raise Refused('qualified_function_preimage','terminal')
                if mod_body.get('mutable')!=body.get('globals'):
                    raise Refused('qualified_function_global_alias','terminal')
                for name in ('globals','annotations','builtins'):
                    binding=body.get(name)
                    if type(binding) is not int or not 0<=binding<len(values) or values[binding][0]!='dict':
                        raise Refused('qualified_function_actual_bindings','terminal')
            if kind=='qualified-module':
                pin=pins.get(body.get('file'))
                if pin is None or body.get('preimage_sha256')!=pin.get('sha256') or body.get('preimage_bytes')!=pin.get('bytes'):
                    raise Refused('qualified_module_preimage','terminal')
                link=body.get('mutable')
                if type(link) is not int or not 0<=link<len(values) or values[link][0]!='dict':
                    raise Refused('qualified_module_mutable','terminal')
            if kind=='fd-collection' and body.get('whole_domain') is not False:
                raise Refused('actor_fd_collection_collapsed','terminal')
        for row,root in zip(rows,graph['result_root_indexes']):
            exact(row,('identity','class','size','reservation_tokens','cached_full_immutable_sha256','immutable_bytes'),'actor_result_row')
            if any(token not in self.observer.pending for token in row['reservation_tokens']):raise Refused('actor_result_credit','terminal')
            kind,body=values[root]
            if row['immutable_bytes'] is not (kind=='bytes'):raise Refused('actor_result_immutability','terminal')
            if kind in ('bytes','bytearray','str','list','tuple','dict','set','frozenset','fd-collection'):
                size=body['bytes'] if kind in ('bytes','bytearray') else (body['row_count'] if kind=='fd-collection' else len(body))
                if row['size']!=size:raise Refused('actor_result_body_size','terminal')
        domain=full_value_history_domain(arena,graph['FD_domain'])
        if domain['truncated'] is not False or domain['arena_token']!=self.owner_export_hold.token or domain['history_max']!=65536:
            raise Refused('actor_FD_domain_identity','terminal')
        books=history_domain_books(domain)
        actual=[];identities=set();decoded_books=[]
        for book in books:
            if book.get('truncated') is not False:raise Refused('actor_FD_journal_truncated','terminal')
            if 'history_codec' not in book:raise Refused('actor_FD_history_codec','terminal')
            history=history_book_rows(book)
            if len(history)!=book['journal_count']:raise Refused('actor_FD_history_count','terminal')
            decoded_books.append((book,history))
            for row in history:
                key=(row['credit'],row['slot'],row['generation'])
                if key in identities:raise Refused('actor_FD_history_duplicate','terminal')
                identities.add(key);actual.append(row)
                if row['credit'] not in self.observer.pending:raise Refused('actor_FD_history_credit','terminal')
                if row['status']=='PREOWNED':raise Refused('actor_FD_cancellation_unconfirmed','terminal')
            active=sorted(row['fd'] for row in history if row['status'] in ('ACQUIRED','HELD','UNKNOWN','ROOT_PREOWNED_BIND_PENDING','PREOWNED_CHILD_TABLE'))
            if active!=book['pending_fds']:raise Refused('actor_FD_status_correspondence','terminal')
        if len(actual)!=domain['history_count']:raise Refused('actor_FD_history_complete_count','terminal')
        # Full actual collections were joined field-for-field and with typed
        # absence/None to the arena primary bank by validate_full_value_arena.
        # Descriptive journal views select that same complete primary book.
        primary_books={b['collection']['collection_identity']:b for b in books}
        for record in local+held:
            journal=record.get('journal')
            if journal is None:continue
            collection=validate_history_collection(journal.get('collection'),journal.get('journal_count'))
            if collection['domain_identity']!=domain['domain_identity']:
                raise Refused('actor_fd_collection_foreign_primary','terminal')
            primary=primary_books.get(collection['collection_identity'])
            if primary is None or collection!=primary['collection']:
                raise Refused('actor_fd_collection_membership','terminal')
            # Repeated same-object descriptive aliases are allowed only when
            # their complete row/credit/fault view agrees, not by shape/status.
            for name,value in journal.items():
                if name=='history_codec':
                    if value!=primary['history_codec']:raise Refused('actor_complete_journal_alias','terminal')
                elif name not in primary or value!=primary[name]:
                    raise Refused('actor_complete_journal_alias','terminal')
        self.actor_ownership_complete=True

    def _consume_early_stock_prefix(self,request):
        # Consume a v3 stock prefix. Aliases resolve to bodies this Root already holds.
        # A foreign or unsupported entry cannot be reported as a complete body.
        # Validation is structural: this method does not import or execute Source.
        exact(request,('type','owner_pid','prepaid_token','schema','history_codec','sealed_bundle',
            'admitted_source_import','error_arena','alias_roles','complete','error_graph_complete',
            'foreign_unwalked','unsupported','truncated','ownership_retired'),'bootstrap_prefix_schema')
        if request['type']!='bootstrap-owner-prefix' or request['schema'] not in (
                'friday.a190.stock-bootstrap-prefix.v3','friday.sol074.stock-bootstrap-prefix.v4','friday.sol086.stock-bootstrap-prefix.v5','friday.sol090.stock-bootstrap-prefix.v6'):
            raise Refused('bootstrap_prefix_schema','terminal')
        new_attempts=request['schema']=='friday.sol090.stock-bootstrap-prefix.v6'
        physical=request['schema'] in ('friday.sol086.stock-bootstrap-prefix.v5','friday.sol090.stock-bootstrap-prefix.v6')
        if new_attempts:
            attempts=request['admitted_source_import']
            if type(attempts) is not list:
                raise Refused('bootstrap_actual_import_attempts','terminal')
            for row in attempts:
                if (type(row) is not dict or set(row)!={'name','state'}
                        or type(row['name']) is not str
                        or row['state'] not in ('STARTED','COMPLETE','ERROR')):
                    raise Refused('bootstrap_actual_import_attempts','terminal')
        current_bindings=request['schema'] in ('friday.sol074.stock-bootstrap-prefix.v4','friday.sol086.stock-bootstrap-prefix.v5','friday.sol090.stock-bootstrap-prefix.v6')
        if self.owner_export_hold is None or request['owner_pid']!=self.actor_pid or request['prepaid_token']!=self.owner_export_hold.token or not self.frame.last_prepaid:
            raise Refused('bootstrap_prefix_owner','terminal')
        if (request['truncated'] is not False or request['ownership_retired'] is not False
                or (not new_attempts and type(request['admitted_source_import']) is not bool)
                or (not physical and request['admitted_source_import'] is not False)):
            raise Refused('bootstrap_prefix_schema','terminal')
        # Physical v5 also retains a later admitted-loader import-error prefix.
        # Its diagnostic bit never substitutes for complete body/preimage/
        # foreign/unsupported validators below, nor proves native finite end.
        if self.bootstrap_prefix_graph is not None:raise Refused('bootstrap_prefix_duplicate','terminal')
        complete=request['complete'];error_complete=request['error_graph_complete']
        foreign=request['foreign_unwalked'];unsupported=request['unsupported']
        if type(complete) is not bool or type(error_complete) is not bool or type(foreign) is not list or type(unsupported) is not list:
            raise Refused('bootstrap_prefix_schema','terminal')
        if complete is True and (foreign or unsupported or error_complete is not True):
            raise Refused('bootstrap_prefix_false_complete','terminal')
        if error_complete is True and unsupported:
            raise Refused('bootstrap_prefix_false_complete','terminal')
        for row in foreign:
            exact(row,('module','class','name','reason'),'bootstrap_prefix_foreign')
            if type(row['module']) is not str or type(row['class']) is not str or type(row['reason']) is not str:
                raise Refused('bootstrap_prefix_foreign','terminal')
            if row['name'] is not None and type(row['name']) is not str:raise Refused('bootstrap_prefix_foreign','terminal')
        for row in unsupported:
            exact(row,('module','class','reason'),'bootstrap_prefix_unsupported')
            if type(row['module']) is not str or type(row['class']) is not str or type(row['reason']) is not str:
                raise Refused('bootstrap_prefix_unsupported','terminal')
        sealed=request['sealed_bundle']
        if sealed is not None:
            if type(sealed) is not dict or sealed.get('encoding')!='parent-sealed-memfd':
                raise Refused('bootstrap_prefix_bundle','terminal')
            if type(self.sealed_bundle) is not bytes or sealed.get('bytes')!=len(self.sealed_bundle):
                raise Refused('bootstrap_prefix_bundle','terminal')
            if sealed.get('sha256')!=hashlib.sha256(self.sealed_bundle).hexdigest():
                raise Refused('bootstrap_prefix_bundle','terminal')
            self.bundle_hold.commit(hash_bytes=len(self.sealed_bundle))
        try:decode_history_rows(request['history_codec'])
        except Refused:raise Refused('bootstrap_prefix_history','terminal')
        arena=request['error_arena']
        fields=('schema','roots','node_count','chunks','truncated')+(('physical_body',) if physical else ())
        exact(arena,fields,'bootstrap_prefix_arena')
        schema='friday.sol086.early-stock-prefix-arena.v2' if physical else 'friday.a190.early-stock-prefix-arena.v1'
        if arena['schema']!=schema or arena['truncated'] is not False:
            raise Refused('bootstrap_prefix_arena','terminal')
        count=arena['node_count']
        if type(count) is not int or type(count) is bool or not 1<=count<=262144:
            raise Refused('bootstrap_prefix_arena','terminal')
        if type(arena['chunks']) is not list:raise Refused('bootstrap_prefix_arena','terminal')
        nodes=[]
        for chunk in arena['chunks']:
            if type(chunk) is not list or len(chunk)>512:raise Refused('bootstrap_prefix_arena','terminal')
            nodes.extend(chunk)
        if len(nodes)!=count:raise Refused('bootstrap_prefix_arena','terminal')
        if physical:self.actor_body_carrier.validate_spans(nodes,arena['physical_body'])
        roles=request['alias_roles']
        exact(roles,('sealed-bundle-bytes','decoded-sealed-bundle','pre-exec-launch','active-error'),'bootstrap_prefix_alias')
        def index(value):
            if type(value) is not int or type(value) is bool or not 0<=value<count:
                raise Refused('bootstrap_prefix_reference','terminal')
            return value
        if type(arena['roots']) is not list or not arena['roots']:raise Refused('bootstrap_prefix_arena','terminal')
        for item in arena['roots']:index(item)
        source_modules=frozenset(('actor_context','admission','capacity','class_semantics','common',
            'consumer_bridge','custody','extraction','fact_bridge','independent_selector','launcher','lifetime',
            'native','normalization','observer','operations','retention','roles','root_tool_adapter','snapshot_producer',
            'actor_bootstrap','authority','bill','body_scope','canonical','capability','contract','declared_controls',
            'document_vector','document_windows','effects','fixtures','formats','ingress','material_literals',
            'performing_contracts','pins','producer_consumer','receipt_chain','recipe_planner','resource_meter',
            'runtime_consumer','schema_validate','semantics','whole_join'))
        code_fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags',
            'co_code','co_consts','co_names','co_varnames','co_filename','co_name','co_qualname','co_firstlineno',
            'co_linetable','co_exceptiontable','co_freevars','co_cellvars')
        saw_foreign=False;saw_unsupported=False;aliases={};parsed=None
        def bundle_obj():
            nonlocal parsed
            if parsed is None:
                if type(self.sealed_bundle) is not bytes:raise Refused('bootstrap_prefix_bundle','terminal')
                try:parsed=json.loads(self.sealed_bundle.decode('ascii'))
                except (UnicodeError,ValueError):raise Refused('bootstrap_prefix_bundle','terminal')
            return parsed
        def hex_text(value,size):
            if type(value) is not str or len(value)!=size or any(c not in '0123456789abcdef' for c in value):
                raise Refused('bootstrap_prefix_bytes','terminal')
        for slot,row in enumerate(nodes):
            if type(row) is not list or len(row)!=2:raise Refused('bootstrap_prefix_row','terminal')
            kind,body=row
            if kind=='none' or kind in ('ellipsis','token-missing'):
                if body is not None:raise Refused('bootstrap_prefix_scalar','terminal')
            elif kind=='bool':
                if type(body) is not bool:raise Refused('bootstrap_prefix_scalar','terminal')
            elif kind=='int':
                if type(body) is not str or not 1<=len(body)<=4096:raise Refused('bootstrap_prefix_integer','terminal')
                try:
                    if str(int(body))!=body:raise Refused('bootstrap_prefix_integer','terminal')
                except ValueError:raise Refused('bootstrap_prefix_integer','terminal')
            elif kind=='str':
                if type(body) is not str:raise Refused('bootstrap_prefix_scalar','terminal')
            elif kind=='float64':
                hex_text(body,16)
            elif kind in ('bytes','bytearray'):
                if physical:
                    exact(body,('offset','bytes'),'bootstrap_prefix_bytes')
                else:
                    if type(body) is not list:raise Refused('bootstrap_prefix_bytes','terminal')
                    for part in body:
                        if type(part) is not str or len(part)>32768 or len(part)%2:raise Refused('bootstrap_prefix_bytes','terminal')
                        hex_text(part,len(part))
            elif kind in ('list','tuple','set','frozenset','range','slice'):
                if type(body) is not list:raise Refused('bootstrap_prefix_sequence','terminal')
                if kind in ('range','slice') and len(body)!=3:raise Refused('bootstrap_prefix_sequence','terminal')
                for item in body:index(item)
                if kind in ('set','frozenset') and len(set(body))!=len(body):raise Refused('bootstrap_prefix_sequence','terminal')
            elif kind in ('dict','context'):
                if type(body) is not list:raise Refused('bootstrap_prefix_dict','terminal')
                for pair in body:
                    if type(pair) is not list or len(pair)!=2:raise Refused('bootstrap_prefix_dict','terminal')
                    index(pair[0]);index(pair[1])
            elif kind=='context-var':
                exact(body,('name','has_default','default','present','current'),'bootstrap_prefix_context_var')
                if type(body['name']) is not str or type(body['has_default']) is not bool or type(body['present']) is not bool:
                    raise Refused('bootstrap_prefix_context_var','terminal')
                index(body['default']);index(body['current'])
            elif kind=='context-token':
                exact(body,('var','old_value','transition'),'bootstrap_prefix_context_token')
                index(body['var']);index(body['old_value'])
                if body['transition'] is None:
                    if error_complete is True:raise Refused('bootstrap_prefix_false_complete','terminal')
                else:index(body['transition'])
            elif kind=='code-body':
                exact(body,code_fields,'bootstrap_prefix_code')
                for item in body.values():index(item)
            elif kind=='error':
                exact(body,('module','class','args','attributes','cause','context','suppress_context','traceback','notes'),'bootstrap_prefix_error')
                if type(body['module']) is not str or type(body['class']) is not str or type(body['suppress_context']) is not bool:
                    raise Refused('bootstrap_prefix_error','terminal')
                for name in ('args','attributes','cause','context','traceback','notes'):index(body[name])
            elif kind=='traceback':
                exact(body,('next','frame','line','lasti'),'bootstrap_prefix_traceback')
                index(body['next']);index(body['frame'])
                if body['line'] is not None and type(body['line']) is not int:raise Refused('bootstrap_prefix_traceback','terminal')
                if type(body['lasti']) is not int or type(body['lasti']) is bool:raise Refused('bootstrap_prefix_traceback','terminal')
            elif kind=='frame':
                exact(body,('filename','name','line','locals','globals','code','lasti','trace','trace_lines','trace_opcodes'),'bootstrap_prefix_frame')
                if type(body['filename']) is not str or type(body['name']) is not str:raise Refused('bootstrap_prefix_frame','terminal')
                if body['line'] is not None and type(body['line']) is not int:raise Refused('bootstrap_prefix_frame','terminal')
                if type(body['lasti']) is not int or type(body['lasti']) is bool:raise Refused('bootstrap_prefix_frame','terminal')
                if type(body['trace_lines']) is not bool or type(body['trace_opcodes']) is not bool:raise Refused('bootstrap_prefix_frame','terminal')
                for name in ('locals','globals','code','trace'):index(body[name])
            elif kind=='preowned-alias':
                exact(body,('role','bytes','sha256','keys'),'bootstrap_prefix_alias')
                if body['role'] not in ('sealed-bundle-bytes','decoded-sealed-bundle','pre-exec-launch'):
                    raise Refused('bootstrap_prefix_alias','terminal')
                if body['role'] in aliases:raise Refused('bootstrap_prefix_alias','terminal')
                aliases[body['role']]=slot
                if type(body['bytes']) is not int or type(body['bytes']) is bool or body['bytes']<0:
                    raise Refused('bootstrap_prefix_alias','terminal')
                hex_text(body['sha256'],64)
            elif kind=='preowned-span':
                exact(body,('role','start','bytes','sha256'),'bootstrap_prefix_span')
                if body['role']!='inside-sealed-bundle':raise Refused('bootstrap_prefix_span','terminal')
                if type(self.sealed_bundle) is not bytes:raise Refused('bootstrap_prefix_span','terminal')
                if type(body['start']) is not int or type(body['start']) is bool or type(body['bytes']) is not int or type(body['bytes']) is bool:
                    raise Refused('bootstrap_prefix_span','terminal')
                if body['start']<0 or body['bytes']<0 or body['start']+body['bytes']>len(self.sealed_bundle):
                    raise Refused('bootstrap_prefix_span','terminal')
                blob=self.sealed_bundle[body['start']:body['start']+body['bytes']]
                if hashlib.sha256(blob).hexdigest()!=body['sha256']:raise Refused('bootstrap_prefix_span','terminal')
            elif kind=='foreign-unwalked':
                exact(body,('module','class','name'),'bootstrap_prefix_foreign')
                if type(body['module']) is not str or type(body['class']) is not str:raise Refused('bootstrap_prefix_foreign','terminal')
                if body['name'] is not None and type(body['name']) is not str:raise Refused('bootstrap_prefix_foreign','terminal')
                saw_foreign=True
            elif kind=='unsupported':
                exact(body,('module','class','reason'),'bootstrap_prefix_unsupported')
                saw_unsupported=True
                if error_complete is True:raise Refused('bootstrap_prefix_false_complete','terminal')
            elif kind=='function-body':
                fields=('module','qualname','defaults','kwdefaults','function_dict','closure','closure_empty','code')
                if current_bindings:fields+=('globals','annotations','builtins','closure_cells')
                exact(body,fields,'bootstrap_prefix_function')
                if body['module'] is not None and type(body['module']) is not str:raise Refused('bootstrap_prefix_function','terminal')
                if type(body['qualname']) is not str or type(body['closure_empty']) is not list:raise Refused('bootstrap_prefix_function','terminal')
                if any(type(item) is not bool for item in body['closure_empty']):raise Refused('bootstrap_prefix_function','terminal')
                closure=index(body['closure'])
                if nodes[closure][0]!='tuple' or len(nodes[closure][1])!=len(body['closure_empty']):
                    raise Refused('bootstrap_prefix_function','terminal')
                for name in ('defaults','kwdefaults','function_dict','code'):index(body[name])
                if current_bindings:
                    for name in ('globals','annotations','builtins'):
                        index(body[name])
                        if nodes[body[name]][0]!='dict':raise Refused('bootstrap_prefix_function_bindings','terminal')
                    index(body['closure_cells'])
                    cells=nodes[body['closure_cells']]
                    if cells!=['none',None]:
                        if cells[0]!='tuple' or len(cells[1])!=len(body['closure_empty']):raise Refused('bootstrap_prefix_function_cells','terminal')
                        for cell_id,value_id,empty in zip(cells[1],nodes[closure][1],body['closure_empty']):
                            index(cell_id)
                            if nodes[cell_id][0]!='closure-cell':raise Refused('bootstrap_prefix_function_cells','terminal')
                            cell_body=nodes[cell_id][1]
                            if cell_body['empty']!=empty or cell_body['contents']!=value_id:raise Refused('bootstrap_prefix_function_cell_alias','terminal')
                    elif body['closure_empty']:raise Refused('bootstrap_prefix_function_cells','terminal')
            elif kind=='closure-cell':
                if not current_bindings:raise Refused('bootstrap_prefix_kind','terminal')
                exact(body,('empty','contents'),'bootstrap_prefix_closure_cell');index(body['contents'])
                if type(body['empty']) is not bool or body['empty'] and nodes[body['contents']]!=['none',None]:
                    raise Refused('bootstrap_prefix_closure_cell','terminal')
            elif kind=='bound-method':
                exact(body,('self','function'),'bootstrap_prefix_method');index(body['self']);index(body['function'])
            elif kind in ('staticmethod','classmethod'):
                exact(body,('function','attributes'),'bootstrap_prefix_method');index(body['function']);index(body['attributes'])
            elif kind=='property':
                exact(body,('get','set','delete','doc'),'bootstrap_prefix_property')
                for name in ('get','set','delete','doc'):index(body[name])
            elif kind=='memoryview':
                exact(body,('object','full_bytes','format','shape','strides','readonly'),'bootstrap_prefix_memoryview')
                if type(body['format']) is not str or type(body['readonly']) is not bool:raise Refused('bootstrap_prefix_memoryview','terminal')
                for name in ('object','full_bytes','shape','strides'):index(body[name])
            elif kind=='module-bindings':
                exact(body,('module','file','mutable','foreign_names','preimage'),'bootstrap_prefix_module')
                if type(body['module']) is not str or body['module'] not in source_modules and body['module'] not in ('__main__','actor_bootstrap'):
                    raise Refused('bootstrap_prefix_module','terminal')
                if body['file'] is not None and type(body['file']) is not str:raise Refused('bootstrap_prefix_module','terminal')
                if type(body['foreign_names']) is not list or any(type(name) is not str for name in body['foreign_names']):
                    raise Refused('bootstrap_prefix_module','terminal')
                if body['foreign_names']:saw_foreign=True
                index(body['mutable'])
                if body['preimage'] is not None:
                    exact(body['preimage'],('sha256','bytes'),'bootstrap_prefix_preimage')
                    hex_text(body['preimage']['sha256'],64)
                    found=False
                    for source_row in bundle_obj().get('sources') or ():
                        if type(source_row) is not dict or source_row.get('path')!=body['file']:continue
                        if type(source_row.get('source')) is not str:break
                        blob=source_row['source'].encode('utf-8')
                        digest=hashlib.sha256(blob).hexdigest()
                        found=digest==body['preimage']['sha256'] and len(blob)==body['preimage']['bytes'] and digest==source_row.get('sha256')
                        break
                    if found is not True:raise Refused('bootstrap_prefix_preimage','terminal')
            elif kind=='qualified-class':
                exact(body,('module','qualname','fields','foreign_names'),'bootstrap_prefix_class')
                if body['module'] is not None and type(body['module']) is not str:raise Refused('bootstrap_prefix_class','terminal')
                if type(body['qualname']) is not str:raise Refused('bootstrap_prefix_class','terminal')
                if type(body['foreign_names']) is not list or any(type(name) is not str for name in body['foreign_names']):
                    raise Refused('bootstrap_prefix_class','terminal')
                if body['foreign_names']:saw_foreign=True
                index(body['fields'])
            elif kind=='bootstrap-object':
                exact(body,('class','state'),'bootstrap_prefix_bootstrap')
                if body['class'] not in ('StockChildJournal','StockChannel','VerifiedSource'):
                    raise Refused('bootstrap_prefix_bootstrap','terminal')
                index(body['state'])
            elif kind=='source-object':
                exact(body,('module','class','state'),'bootstrap_prefix_source_object')
                if body['module'] not in source_modules or type(body['class']) is not str:
                    raise Refused('bootstrap_prefix_source_object','terminal')
                index(body['state'])
            else:
                raise Refused('bootstrap_prefix_kind','terminal')
        if saw_unsupported and not unsupported:raise Refused('bootstrap_prefix_false_complete','terminal')
        if saw_foreign and not foreign:raise Refused('bootstrap_prefix_false_complete','terminal')
        if (saw_foreign or foreign) and complete is True:raise Refused('bootstrap_prefix_false_complete','terminal')
        if aliases.get('sealed-bundle-bytes')!=roles['sealed-bundle-bytes'] or aliases.get('decoded-sealed-bundle')!=roles['decoded-sealed-bundle'] or aliases.get('pre-exec-launch')!=roles['pre-exec-launch']:
            raise Refused('bootstrap_prefix_alias','terminal')
        if (sealed is None)!=(roles['sealed-bundle-bytes'] is None):raise Refused('bootstrap_prefix_bundle','terminal')
        active=roles['active-error']
        if type(active) is not int or active not in arena['roots'] or nodes[index(active)][0]!='error':
            raise Refused('bootstrap_prefix_error','terminal')
        if roles['sealed-bundle-bytes'] is not None:
            body=nodes[roles['sealed-bundle-bytes']][1]
            if body['keys'] is not None or body['sha256']!=hashlib.sha256(self.sealed_bundle).hexdigest() or body['bytes']!=len(self.sealed_bundle):
                raise Refused('bootstrap_prefix_bundle','terminal')
        if roles['decoded-sealed-bundle'] is not None:
            body=nodes[roles['decoded-sealed-bundle']][1]
            parsed_bundle=bundle_obj()
            if type(parsed_bundle) is not dict or body['sha256']!=hashlib.sha256(self.sealed_bundle).hexdigest() or body['bytes']!=len(self.sealed_bundle):
                raise Refused('bootstrap_prefix_bundle','terminal')
            if body['keys']!=sorted(key for key in parsed_bundle if type(key) is str) or len(body['keys'])!=len(parsed_bundle):
                raise Refused('bootstrap_prefix_bundle','terminal')
            self.prefix_decoded_bundle=parsed_bundle
        if roles['pre-exec-launch'] is not None:
            if self.actual_launch_fact is None:raise Refused('bootstrap_prefix_launch_body','terminal')
            try:
                encoded=json.dumps(self.actual_launch_fact,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
            except (TypeError,ValueError):
                raise Refused('bootstrap_prefix_launch_body','terminal')
            body=nodes[roles['pre-exec-launch']][1]
            if hashlib.sha256(encoded).hexdigest()!=body['sha256'] or len(encoded)!=body['bytes'] or body['keys'] is not None:
                raise Refused('bootstrap_prefix_launch_body','terminal')
            self.prefix_consumed_launch=self.actual_launch_fact
        self.bootstrap_prefix_graph=request
        self.prefix_foreign_unwalked=tuple(row['reason'] for row in foreign)
        self.actor_ownership_complete=False
        return {'retained':True,'complete':current_bindings and complete is True,
            'selected_body_consumed':current_bindings and error_complete is True and not unsupported and not foreign,
            'ownership_retired':False}

    def _control(self,request):
        kind=request["type"]
        if kind=='bootstrap-owner-prefix':
            if request.get('owner_pid')!=self.actor_pid or request.get('prepaid_token')!=self.owner_export_hold.token or not self.frame.last_prepaid:
                raise Refused('bootstrap_prefix_owner','terminal')
            schema=request.get('schema')
            if schema=='friday.a190.stock-bootstrap-prefix.v1':
                if request.get('complete') is not False or request.get('truncated') is not False:
                    raise Refused('bootstrap_prefix_schema','terminal')
                if self.bootstrap_prefix_graph is not None:raise Refused('bootstrap_prefix_duplicate','terminal')
                self.bootstrap_prefix_graph=request
                self.actor_ownership_complete=False
                return {'retained':True,'complete':False,'ownership_retired':False}
            if schema in ('friday.a190.stock-bootstrap-prefix.v3','friday.sol074.stock-bootstrap-prefix.v4','friday.sol086.stock-bootstrap-prefix.v5','friday.sol090.stock-bootstrap-prefix.v6'):
                return self._consume_early_stock_prefix(request)
            if schema!='friday.a190.stock-bootstrap-prefix.v2' or request.get('complete') is not True or request.get('truncated') is not False or request.get('ownership_retired') is not False:
                raise Refused('bootstrap_prefix_schema','terminal')
            sealed=request.get('sealed_bundle')
            if type(sealed) is not dict or sealed.get('encoding')!='parent-sealed-memfd':
                raise Refused('bootstrap_prefix_bundle','terminal')
            if type(self.sealed_bundle) is not bytes or sealed.get('bytes')!=len(self.sealed_bundle) or sealed.get('sha256')!=self.sealed_bundle_sha256:
                raise Refused('bootstrap_prefix_bundle','terminal')
            self.bundle_hold.commit(hash_bytes=len(self.sealed_bundle))
            if hashlib.sha256(self.sealed_bundle).hexdigest()!=sealed.get('sha256'):
                raise Refused('bootstrap_prefix_bundle','terminal')
            try:
                decode_history_rows(request.get('history_codec'))
            except Refused:
                raise Refused('bootstrap_prefix_history','terminal')
            frames=request.get('traceback_frames')
            if type(frames) is not list:raise Refused('bootstrap_prefix_frame','terminal')
            allowed={'alias:sealed-bundle-bytes','alias:decoded-sealed-bundle','alias:pre-exec-launch','alias:child-book','alias:active-error'}
            saw_decoded=False;saw_launch=False
            for frame in frames:
                if type(frame) is not dict:raise Refused('bootstrap_prefix_frame','terminal')
                locals_=frame.get('locals')
                if type(locals_) is not dict:raise Refused('bootstrap_prefix_frame','terminal')
                for item in locals_.values():
                    if type(item) is str and item.startswith('alias:'):
                        if item not in allowed:raise Refused('bootstrap_prefix_alias','terminal')
                        if item=='alias:decoded-sealed-bundle':saw_decoded=True
                        if item=='alias:pre-exec-launch':saw_launch=True
            if (request.get('decoded_bundle')=='alias-of-sealed-body') is not saw_decoded:
                raise Refused('bootstrap_prefix_alias','terminal')
            if (request.get('pre_exec_launch')=='alias-of-retained-launch') is not saw_launch:
                raise Refused('bootstrap_prefix_alias','terminal')
            error=request.get('error')
            if type(error) is not dict or any(name not in error for name in ('module','class','args','text')):
                raise Refused('bootstrap_prefix_error','terminal')
            if type(error['module']) is not str or type(error['class']) is not str or type(error['text']) is not str:
                raise Refused('bootstrap_prefix_error','terminal')
            args=error['args']
            if args is not None and (type(args) is not list or any(item is not None and type(item) not in (bool,int,str) for item in args)):
                raise Refused('bootstrap_prefix_error','terminal')
            root_response=request.get('root_response')
            if root_response is not None:
                if type(root_response) is not dict or root_response.get('encoding')!='retained-control-raw':
                    raise Refused('bootstrap_prefix_control','terminal')
                width=root_response.get('bytes');digest=root_response.get('sha256')
                if type(width) is not int or type(width) is bool or width<0 or width>65536:
                    raise Refused('bootstrap_prefix_control','terminal')
                if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                    raise Refused('bootstrap_prefix_control','terminal')
            if self.bootstrap_prefix_graph is not None:raise Refused('bootstrap_prefix_duplicate','terminal')
            self.bootstrap_prefix_graph=request
            self.actor_ownership_complete=False
            return {'retained':True,'complete':True,'ownership_retired':False}

        if kind in ('owner-body-begin','owner-body-chunk','owner-body-end'):
            if (request.get('owner_pid')!=self.actor_pid
                    or request.get('prepaid_token')!=getattr(self.owner_export_hold,'token',None)
                    or not self.frame.last_prepaid):
                raise Refused('prepared_body_actual_actor_channel','terminal')
            base=('type','owner_pid','prepaid_token')
            body=self.actor_body_carrier
            if kind=='owner-body-begin':
                exact(request,base,'prepared_body_begin')
                if getattr(body,'actor_started',False):raise Refused('prepared_body_actor_once','terminal')
                body.actor_started=True
                binding=body.binding()
                credit=self.observer.pending.get(self.owner_export_hold.token)
                if credit is None:raise Refused('prepared_body_wire_credit','terminal')
                binding.update(wire_reads=credit['reads'],wire_output=credit['output'])
                return binding
            if not getattr(body,'actor_started',False):raise Refused('prepared_body_actor_before_begin','terminal')
            if kind=='owner-body-chunk':
                exact(request,base+('offset','body'),'prepared_body_chunk')
                raw=request['body']
                if (request['offset']!=body.count or type(raw) is not str or len(raw)>65536
                        or len(raw)%2 or any(ch not in '0123456789abcdef' for ch in raw)):
                    raise Refused('prepared_body_actual_chunk','terminal')
                body.prospective(len(raw)//2)
                body.append(bytes.fromhex(raw))
                return {'bytes':body.count}
            exact(request,base+('bytes',),'prepared_body_end')
            if type(request['bytes']) is not int or request['bytes']!=body.count:
                raise Refused('prepared_body_actual_end','terminal')
            return body.finish()

        if kind=="actor-owner-state":
            exact(request,("type","sequence","owner_pid","graph","prepaid_token"),"actor_owner_state")
            if request["owner_pid"]!=self.actor_pid or type(request["sequence"]) is not int or request["sequence"]<=self.actor_local_sequence:
                raise Refused("actor_owner_state_identity")
            if self.owner_export_hold is None or request['prepaid_token']!=self.owner_export_hold.token or not self.frame.last_prepaid:
                raise Refused('actor_owner_prepaid_identity','terminal')
            graph=request['graph']
            exact(graph,('schema','owner_pid','local_count','local_chunks','held_count','held_chunks','results','FD_domain','value_arena','local_root_indexes','held_root_indexes','result_root_indexes','error_root_indexes','truncated'),'complete_actor_owner_graph')
            if graph['schema']!='friday.a181.complete-actor-owner-graph.v1' or graph['owner_pid']!=self.actor_pid or graph['truncated'] is not False:
                raise Refused('complete_actor_owner_graph_identity','terminal')
            # Retain the exact received body even when nested completion
            # refuses. Never replace the full earlier graph with an ACK label.
            self.actor_local_sequence=request['sequence'];self.actor_local_graph=graph
            self._validate_actor_completion(graph)
            return {"retained":True,"sequence":self.actor_local_sequence,"complete":True}
        if kind=="material-acquire":
            exact(request,("type","name"),"material_acquire")
            name=request["name"]
            if not self.material_phase or name not in ALL15[:12] or name in self.operation_windows:raise Refused("material_acquire_order")
            self.current_operation=name
            self.operation_windows[name]={"started_ns":mono(),"input_body":None,"stdout_at":len(self.capture_out),"stderr_at":len(self.capture_err),"material_acquisition":True}
            return {}
        if kind=="material-current":
            exact(request,("type","name"),"material_current")
            if not self.material_phase or request["name"] not in self.operation_windows:raise Refused("material_acquisition_owner")
            self.current_operation=request["name"];return {}
        if kind=="legacy-closure":
            exact(request,("type","role","pin"),"legacy_closure")
            if not self.material_phase or request["role"] not in ("data","native") or request["role"] in self.legacy_closures:raise Refused("legacy_closure_owner")
            self.check_owned_path(request["pin"]["path"])
            with Held(request["pin"]["path"],request["pin"],self.observer,INPUT_MAX,True):pass
            self.legacy_closures[request["role"]]=request["pin"];return {}
        if kind=="phase-independent-ordinary":
            exact(request,("type",),"phase_selection")
            if not self.material_phase or set(self.operation_windows)!=set(ALL15[:12]) or set(self.legacy_closures)!={"native","data"}:raise Refused("full_material_phase")
            from fact_bridge import fresh_graph,selected_phase
            self.pack_material_records()
            record=self.store.put("complete-material-phase-graph",canonical(fresh_graph(self),self.observer,OUTPUT_MAX),"member")
            selected=selected_phase(self,record["pin"])
            # Every complete observed/expected typed body is now independent,
            # not copied into expected by this producer. Later operation input
            # computation must use this exact material phase.
            self.material_phase=False
            self.page_files=list(self.phase_ordinary.pages)
            self.page_sequence=list(self.phase_ordinary.context["page_sequence"] if self.phase_ordinary.context["page_sequence"] is not None else self.phase_ordinary.context["streams"])
            return {"ordinary":selected["ordinary"],"sources":selected["sources"]}
        if kind=="reserve":
            exact(request,("type","purpose","reads","output","allocation","slots","hash_bytes"))
            r=self.observer.reserve(request["purpose"],request["reads"],request["output"],request["allocation"],request["slots"],request["hash_bytes"])
            self.observer.pending[r.token]["owner_pid"]=self.actor_pid
            return {"token":r.token}
        if kind=="commit":
            exact(request,("type","token","reads","output","hash_bytes"));self.observer.commit(request["token"],request["reads"],request["output"],request["hash_bytes"]);return {}
        if kind=="grow":
            exact(request,("type","token","allocation"));self.observer.grow(request["token"],request["allocation"]);return {}
        if kind=="release":
            exact(request,("type","token"));return {'released':self.observer.release(request['token']) is True}
        if kind=="partial":
            exact(request,("type","path","size"));self.check_owned_path(request["path"]);self.observer.note_partial(request["path"],request["size"]);return {}
        if kind=="cleanup":
            exact(request,("type","cause"));self.observer.note_cleanup(request["cause"]);return {}
        if kind=="begin":
            exact(request,("type","name","input_body"));name=request["name"]
            if self.material_phase or name not in ALL15:raise Refused("operation")
            previous=self.operation_windows.get(name)
            if previous is not None and previous["input_body"] is not None:raise Refused("operation")
            self.current_operation=name
            self.operation_windows[name]={"started_ns":previous["started_ns"] if previous else mono(),"input_body":request["input_body"],
                "stdout_at":previous["stdout_at"] if previous else len(self.capture_out),"stderr_at":previous["stderr_at"] if previous else len(self.capture_err)}
            return {"started_ns":self.operation_windows[name]["started_ns"]}
        if kind=="native":
            exact(request,("type","operation","verb","inputs"))
            if request["operation"]!=self.current_operation:raise Refused("native_operation")
            return self.native.perform(request["operation"],request["verb"],request["inputs"])
        if kind=="operation-window":
            exact(request,("type","name"),"control_exact_fields")
            if request["name"]!=self.current_operation:raise Refused("operation_window_owner")
            return {"started_ns":self.operation_windows[request["name"]]["started_ns"]}
        if kind=="snapshot-observed":
            exact(request,("type","abi"),"control_exact_fields")
            import datetime
            return {"created_utc":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "creation_tool_sha256":domain("friday.a128.full-tool.v1",self.actual_tool(request["abi"]),self.observer)}
        if kind=="actual-creation-tool-preimage":
            exact(request,("type","abi"),"control_exact_fields")
            tool=self.actual_tool(request["abi"])
            raw=b"friday.a128.full-tool.v1\0"+canonical(tool,self.observer)
            name="actual-creation-tool-"+str(self.counter);self.counter+=1
            record=self.store.put(name,raw,"native",name)
            return {"pin":record["pin"],"actual_domain_sha256":sha(raw,self.observer)}
        if kind=="final-independent-ordinary":
            exact(request,("type","public_preimage_pin"),"control_exact_fields")
            pin=request["public_preimage_pin"];self.check_owned_path(pin["path"])
            with Held(pin["path"],pin,self.observer,OUTPUT_MAX,True) as held:
                held.check()
            self.retained_receipts.append({"section":"final-public","target":self.case["case_id"],"record":{"pin":pin},"retained_ns":mono()})
            from independent_selector import selected_after_retention
            selected=selected_after_retention(self,"final-public",self.case["case_id"],pin,"final")
            from roles import validate_ordinary
            validate_ordinary(selected["ordinary"],self.enrollment,self.observer)
            final=OrdinaryInput(selected["ordinary"],selected["sources"],self.observer,self.consumer,self.actor_id)
            final.golden_check()
            if final.selected["case_id"]!=self.case["case_id"] or final.selected["selector_id"]!=self.selector_id:
                raise Refused("final_original_case_selection")
            produced={r["target"]:r["record"]["pin"]["sha256"] for r in self.retained_receipts if r["section"]=="operations"}
            final_ops=final.context["performing_contracts"]["operations"]
            if {r["target"]:r["sha256"] for r in final_ops}!=produced or set(produced)!=set(ALL15):raise Refused("final_all15_actual_root_receipts")
            from fact_bridge import validate_final
            validate_final(self,final,pin)
            return {"ordinary":selected["ordinary"],"sources":selected["sources"]}
        if kind=="independently-selected-generated-member":
            exact(request,("type","operation","public_preimage_pin"),"control_exact_fields")
            name=request["operation"]
            if name!=self.current_operation:raise Refused("generated_operation_owner")
            plans=[r for r in self.admission["members"][name] if r["generated"] is True]
            if len(plans)!=1:raise Refused("generated_member_census")
            plan=plans[0];pin=request["public_preimage_pin"];self.check_owned_path(pin["path"])
            with Held(pin["path"],pin,self.observer,INPUT_MAX,True):pass
            if pin["bytes"]>plan["size"]:raise Refused("generated_declared_capacity")
            index="generated-"+str(len(self.selector_receipts))
            self.retained_receipts.append({"section":"generated-member","target":name,"record":{"pin":pin},"retained_ns":mono()})
            from independent_selector import selected_after_retention
            selected=selected_after_retention(self,"generated-member",name,pin,index)
            from roles import validate_role
            proposed=selected["generated_member_plan"]
            validate_role(proposed,"plan",self.enrollment,self.observer)
            if {k:v for k,v in proposed.items() if k not in ("sha256","size")}!={k:v for k,v in plan.items() if k not in ("sha256","size")}:
                raise Refused("late_generated_scope_expansion")
            if proposed["sha256"]!=pin["sha256"] or proposed["size"]!=pin["bytes"]:raise Refused("late_generated_full_byte_selection")
            self.admission["members"][name]=[proposed if r is plan else r for r in self.admission["members"][name]]
            self.generated_plans[name]=[dict(proposed)]
            return {"plan":proposed}
        if kind in ("normalize","authentication"):
            exact(request,("type","section","target","event_pin") if kind=="normalize" else
                ("type","operation","record_pin"),"control_exact_fields")
            pin=request["event_pin"] if kind=="normalize" else request["record_pin"]
            self.check_owned_path(pin["path"])
            with Held(pin["path"],pin,self.observer,INPUT_MAX,True) as held:
                event=parse(held.read(INPUT_MAX),self.observer)
            if kind=="authentication":
                self.authentication.setdefault(request["operation"],[]).append(pin);return {}
            receipt=self.normalizer.normalize(request["section"],request["target"],event)
            if not (self.material_phase and request["section"] in ("materials","closures")):
                self.add_page(receipt["record"]["pin"],receipt["record"]["ref"]["kind"],receipt["record"]["ref"]["path"])
            expected=receipt["expected"]
            if expected is not None:self.add_page(expected["pin"],expected["kind"],expected["logical_path"])
            # File metadata crosses the pipe; full observed body is held through
            # its physical preimage, avoiding a second 2M transport allocation.
            return {k:v for k,v in receipt.items() if k!="body"}
        if kind=="page-catalog":
            exact(request,("type",),"control_exact_fields")
            return {"catalog":[self.page_files,self.page_sequence]}
        if kind=="outer":
            exact(request,("type",),"control_exact_fields")
            return {"observation":self.observer.receipt()}
        if kind=="failure":
            exact(request,("type","name","error"),"control_exact_fields")
            self.errors.append(request["error"]);return {}
        if kind=="done":
            exact(request,("type","result_pin","all15","binding"),"control_exact_fields")
            if request["all15"]!=list(ALL15):raise Refused("all15_terminal")
            pin=request["result_pin"];self.check_owned_path(pin["path"])
            with Held(pin["path"],pin,self.observer,OUTPUT_MAX,True) as held:
                self.full_output_pin=held.pin_now()
            if self.admission["launch_mode"]=="retained-consumer":
                ordinary=OrdinaryInput(self.case,self.admission["inputs"],self.observer,self.consumer,self.actor_id)
                self.final_binding=ordinary.full_binding()
                catalog={r["id"]:r for r in self.admission["coverage"]["all69"]}
                expected_output=ordinary.invoke_control(catalog[self.case["case_id"]]) if self.case["case_id"] in catalog else ordinary.invoke()
                self.final_expected_output=sha(expected_output,self.observer)
            if request["binding"]!=self.final_binding or self.full_output_pin["sha256"]!=self.final_expected_output:
                raise Refused("full_final_output_and_five_argument_binding","postdelivery")
            return {"retained":True}
        raise Refused("control_schema")

    def launch(self,case_id):
        if self.admission["launch_mode"]=="perform-and-retain" and case_id not in {r["case_id"] for r in self.admission["ordinary"]["positives"]}:
            raise Refused("performing_controls_require_original_retained_consumer_mode")
        chosen=self.selected_case(case_id)
        # Root verifies complete ordinary preimages/golden before effects.
        prepared=OrdinaryInput(chosen,self.admission["inputs"],self.observer,self.consumer,"pending-root-actor")
        prepared.golden_check()
        if self.admission["launch_mode"]=="retained-consumer":
            from retention import verify_independently_selected_retention
            verify_independently_selected_retention(self.admission,prepared,self.observer)
        launch_pin=self.admission["image"]["launcher"]
        source_size=sum(r["bytes"] for r in self.enrollment["source_files"] if r["relative_path"].startswith("source/") and r["relative_path"].endswith(".py"))
        # Full actor graph encode/parse and both receiver arenas precede fork,
        # including error/expired export. This uses the original Root caps.
        from capacity import connected_export_authority
        _export_authority=connected_export_authority()
        if _export_authority['fits_single_wire'] is not True:
            raise Refused('owner_export_single_wire_before_effect','before-effect',_export_authority)
        self.actor_body_carrier=self.store.prepare_full_body('whole-actor-values',self.observer.terminal_hold)
        self.actor_fork_body_carrier=self.store.prepare_full_body('whole-actor-fork-values',self.observer.terminal_hold)
        self.actor_failure_carrier=self.store.prepare_full_body('whole-actor-failure-values',self.observer.terminal_hold)
        self.actor_failure_metadata=self.store.prepare_full_body('whole-actor-failure-metadata',self.observer.terminal_hold)
        self.owner_export_hold=self.observer.reserve('complete-actor-owner-export-before-fork',
            reads=_export_authority['producer_receiver_failure_reads'],output=_export_authority['send_and_failure_output'],
            allocation=_export_authority['canonical_allocation'],hash_bytes=_export_authority['hash_bytes'])
        # Cover decoded strings, parser/bootstrap/import copies and encoder
        # overlap BEFORE building any source_rows list or source string.
        # Image-validation arena is reserved and retired inside validate_image.
        self.bundle_hold=self.observer.reserve("sealed-source-bundle-lifetime",reads=INPUT_MAX*4,
            output=INPUT_MAX,allocation=source_size*16+INPUT_MAX*256+65536,slots=1,hash_bytes=INPUT_MAX*2)
        source_rows=[]
        bootstrap_pin=None
        for pin in self.enrollment["source_files"]:
            if pin["relative_path"].startswith("source/") and pin["relative_path"].endswith(".py"):
                with Held(pin["path"],pin,self.observer,INPUT_MAX,True) as held:
                    raw=held.read(INPUT_MAX)
                source_rows.append({"name":os.path.basename(pin["path"])[:-3],"path":pin["path"],
                    "sha256":pin["sha256"],"source":raw.decode("utf-8")})
                if pin["relative_path"]=="source/actor_bootstrap.py":bootstrap_pin=pin
        if bootstrap_pin is None:raise Refused("performing_bootstrap_absent")
        bundle=canonical({"sources":source_rows,"admission":self.admission,"ordinary":self.case,
            "parent_pid":os.getpid(),"consumer_files":self.enrollment["consumer_files"],
            "bootstrap_hash_token":self.bundle_hold.token,'owner_export_token':self.owner_export_hold.token},self.observer)
        if len(bundle)>INPUT_MAX:raise Refused("complete_source_bundle_capacity")
        self.sealed_bundle=bundle
        self.bundle_hold.commit(hash_bytes=len(bundle))
        self.sealed_bundle_sha256=hashlib.sha256(bundle).hexdigest()
        source_rows=[];raw=None
        # Later fork graph, not the image-validation arena.
        self.actor_graph_hold=self.observer.reserve("Root-owned-before-fork-FD-and-initial-control-graph",allocation=INPUT_MAX*260+65536,slots=11)
        self.fdjournal.grant(self.actor_graph_hold)
        bundle_fd=self.fdjournal.acquire(os.memfd_create,"root-owned-publisher-source",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING,credit=self.bundle_hold,holder="sealed-source-bundle")
        at=0
        while at<len(bundle):
            n=os.write(bundle_fd,memoryview(bundle)[at:])
            if n<=0:raise Refused("source_bundle_write")
            at+=n
            self.bundle_hold.commit(output=n)
        fcntl.fcntl(bundle_fd,fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL)
        with Held(launch_pin["path"],launch_pin,self.observer,DOCUMENT_MAX) as executable:
          with Held(bootstrap_pin["path"],bootstrap_pin,self.observer,INPUT_MAX,True) as bootstrap:
            self.actor_exec_leases=[executable,bootstrap]
            # Install ownership before fork and before ANY fallible post-fork
            # pidfd/cgroup/frame step can unwind these context managers.
            for lease in self.actor_exec_leases:lease.retain_until_terminal(self.observer.owner_pid)
            for pin in self.admission["image"]["launcher_dependencies"]:
                lease=Held(pin["path"],pin,self.observer,DOCUMENT_MAX).__enter__()
                lease.retain_until_terminal(self.observer.owner_pid);self.actor_exec_leases.append(lease)
            # Source fork is a NEW Root-owned launcher process. The independently
            # admitted already-loaded code is the held immutable snapshot; no
            # caller process is relabeled and no client-supplied pipe is used.
            req_r,req_w=self.fdjournal.pair(os.O_CLOEXEC,credit=self.actor_graph_hold)
            rsp_r,rsp_w=self.fdjournal.pair(os.O_CLOEXEC,credit=self.actor_graph_hold)
            out_r,out_w=self.fdjournal.pair(os.O_CLOEXEC|os.O_NONBLOCK,credit=self.actor_graph_hold)
            err_r,err_w=self.fdjournal.pair(os.O_CLOEXEC|os.O_NONBLOCK,credit=self.actor_graph_hold)
            gate_r,gate_w=self.fdjournal.pair(os.O_CLOEXEC,credit=self.actor_graph_hold)
            self.actor_fork_hold=self.observer.reserve('actor-child-table-and-accepted-stock-bootstrap-before-fork',
                reads=_export_authority['producer_receiver_failure_reads'],output=_export_authority['send_and_failure_output'],
                allocation=_export_authority['canonical_allocation']+_export_authority['fork_census_allocation'],
                hash_bytes=_export_authority['hash_bytes'],slots=stock_inherited_upper(self.observer)+4)
            child_r,child_w=self.fdjournal.pair(os.O_CLOEXEC,credit=self.actor_fork_hold)
            child_dup_hold=self.observer.reserve('actor-child-stdio-before-fork',allocation=131072,slots=2)
            # Install the partial actual table before any census/domain failure.
            from owned_prefix_bank import PrefixBodyMailbox
            failure_mailbox=PrefixBodyMailbox.prepare(self.actor_failure_carrier,self.actor_failure_metadata)
            self.actor_fork_owner=ForkOwner.__new__(ForkOwner)
            self.actor_fork_owner.__init__(self.observer,self.actor_fork_hold,
                self.actor_fork_body_carrier.binding(),gate_r,self.observer.deadline,
                failure_mailbox=failure_mailbox)
            self.capture_fds={out_r:self.capture_out,err_r:self.capture_err}
            self.actor_started_ns=mono()
            self.actor_argv=[self.admission["image"]["launcher_virtual_path"],"-I","-S","-B","/proc/self/fd/"+str(bootstrap.fd),
                             str(bundle_fd),str(req_w),str(rsp_r),str(self.owner_export_hold.token)]
            pid=os.fork()
            if pid==0:
                try:
                    self.actor_fork_owner.adopt()
                    child_guard(self.observer.owner_pid)
                    for fd in (req_r,rsp_w,out_r,err_r,gate_w,child_r):self.actor_fork_owner.close_one(fd)
                    dupbook=OwnedFDs(credit=child_dup_hold)
                    self.actor_fork_owner.close_one(1);dupbook.acquire(os.dup2,out_w,1,holder='actor-child-stdout')
                    self.actor_fork_owner.close_one(2);dupbook.acquire(os.dup2,err_w,2,holder='actor-child-stderr')
                    os.set_blocking(1,True);os.set_blocking(2,True)
                    if os.read(gate_r,1)!=b"G":os._exit(126)
                    self.actor_fork_owner.body_gate_seen=True
                    actor_sandbox(self.admission["image"],self.admission["image"]["actor_readonly_roots"],self.store.root,self.actor_graph_hold)
                    # Child code never acquires Root authority. Its parent owns
                    # native verbs, observation, normalization, admission and FDs.
                    for fd in (bundle_fd,bootstrap.fd,req_w,rsp_r):
                        self.actor_fork_owner.keep(fd)
                    keep={0,1,2,bundle_fd,bootstrap.fd,req_w,rsp_r,executable.fd,gate_r,child_w}
                    for fd in tuple(self.actor_fork_owner.fds):
                        if fd not in keep:
                            if fd not in self.actor_fork_owner.failure_mailbox.retained_fds:self.actor_fork_owner.close_one(fd)
                    self.actor_fork_owner.emit(child_w)
                    if os.read(gate_r,1)!=b'A':raise Refused('actor_child_owner_not_accepted','terminal')
                    self.actor_fork_owner.body_owner_ack_seen=True
                    if self.observer.fd_state.unknown():raise Refused('FD_CLOSE_UNCONFIRMED','terminal')
                    os.execve(executable.fd,self.actor_argv,self.actor_env)
                except BaseException as exc:
                    try:
                        os.write(2,(type(exc).__name__+":"+str(exc)).encode("utf-8","backslashreplace"))
                    except BaseException as diagnostic_error:
                        self.actor_fork_owner.failure_errors.append(diagnostic_error)
                    try:
                        self.actor_fork_owner.emit_failure(child_w,exc)
                    except BaseException as export_error:
                        # Strong real error/body aliases, never an accepted end.
                        self.actor_fork_owner.failure_errors.append(export_error)
                        exc.owner_export_error=export_error
                    if self.actor_fork_owner.failure_mailbox.accepted_by_parent():os._exit(1)
                    # Never claim a confirmed transfer from process loss.
                    # Pending Root mailbox acceptance has no finite qualified
                    # child end yet; propagate exact unconfirmed state.
                    raise Refused('native_child_failure_bank_acceptance_end_CODE','terminal') from exc
            self.actor_fork_owner.parent_child_pid=pid
            self.actor_pid=pid
            for lease in self.actor_exec_leases:lease.retain_until_terminal(pid)
            self.actor_pidfd=self.fdjournal.acquire(os.pidfd_open,pid,0,credit=self.actor_graph_hold,holder="actor-pidfd")
            for fd in (req_w,rsp_r,out_w,err_w,gate_r):self.fdjournal.close_one(fd)
            self.fdjournal.close_one(child_w)
            cfd=open_absolute(self.observer.cgroup+"/cgroup.procs",os.O_WRONLY,journal=self.fdjournal)
            try:os.write(cfd,str(pid).encode("ascii"))
            finally:self._close_temp(cfd)
            self.observer.register(pid,self.actor_pidfd)
            actual=proc_start(pid,self.observer)
            self.actor_id=self.enrollment["root_namespace"]+":"+str(pid)+":"+str(actual["start_ticks"])
            self.actor_uid,self.actor_gid=os.getuid(),os.getgid()
            self.actual_launch_fact={"actor_id":self.actor_id,"pid":pid,"started_ns":self.actor_started_ns,
                "actual_parent":self.root_fact,"pidfd_identity9":identity9(os.fstat(self.actor_pidfd)),
                "request_pipe_identity9":identity9(os.fstat(req_r)),"response_pipe_identity9":identity9(os.fstat(rsp_w)),
                "selected_executable_pin":executable.pin_now(),"actual_argv":self.actor_argv,
                "sealed_source_bundle_identity9":identity9(os.fstat(bundle_fd)),
                "qualified_admission_ref":self.qualification["raw_ref"],
                "actual_cgroup":self.observer.cgroup,"Root_created":True,"source_issued_grant":False}
            # Child fork preceded this assignment; transmit the actual launch
            # fact on the Root-created channel after kernel custody is established.
            self.frame=Frame(req_r,rsp_w,self.observer.deadline,self.observer,journal=self.fdjournal,owner_hold=self.owner_export_hold)
            os.write(gate_w,b"G")
            self.actor_fork_graph=receive_fork_owner(child_r,self.actor_fork_hold,self.observer.reserve_deadline,pid,self.observer.owner_pid,
                body_reader=self.actor_fork_body_carrier,ack_fd=gate_w,failure_owner=self.actor_fork_owner)
            if self.actor_fork_graph.get('schema')=='friday.a190.fork-final-failure.v1':
                self.bootstrap_prefix_graph=self.actor_fork_graph
                raise Refused('actor_child_bootstrap_failure','terminal',self.actor_fork_graph)
            self.actual_launch_fact['bootstrap_child_ownership']=self.actor_fork_graph
            os.write(gate_w,b'A')
            # Existing gate remains owned until the actual terminal boundary.
            self.frame.send({"launch_fact":self.actual_launch_fact})
            first_error=None
            try:
                with selectors.PollSelector() as sel:
                    sel.register(req_r,selectors.EVENT_READ,"request")
                    sel.register(out_r,selectors.EVENT_READ,"stdout")
                    sel.register(err_r,selectors.EVENT_READ,"stderr")
                    sel.register(self.actor_pidfd,selectors.EVENT_READ,"terminal")
                    sel.register(child_r,selectors.EVENT_READ,'fork-final-failure')
                    terminal=False
                    while not terminal:
                        from lifetime import receive_preowned_fork_failure
                        bank_failure=receive_preowned_fork_failure(self.actor_fork_owner,pid)
                        if bank_failure is not None:
                            self.bootstrap_prefix_graph=bank_failure;self.actor_ownership_complete=False
                            if os.write(gate_w,b'B')!=1:raise Refused('prepared_fork_body_actual_ack','terminal')
                            if first_error is None:
                                first_error=Refused('actor_child_exec_failure','terminal',bank_failure)
                                self.observer.retain_error_arena(first_error)
                        if first_error is None:
                            try:self.observer.check()
                            except BaseException as exc:
                                first_error=exc;self.observer.retain_error_arena(exc)
                        left=((self.observer.deadline if first_error is not None else self.observer.reserve_deadline)-mono())/1e9
                        if left<=0:raise Refused("launcher_deadline","execution")
                        for key,_ in sel.select(min(left,0.1)):
                            if key.data=='fork-final-failure':
                                packet=receive_fork_owner(child_r,self.actor_fork_hold,self.observer.deadline,pid,self.observer.owner_pid,
                                    final_failure=True,allow_eof=True,body_reader=self.actor_fork_body_carrier,ack_fd=gate_w,failure_owner=self.actor_fork_owner)
                                if packet is not None:
                                    self.bootstrap_prefix_graph=packet;self.actor_ownership_complete=False
                                    if first_error is None:
                                        first_error=Refused('actor_child_exec_failure','terminal',packet)
                                        self.observer.retain_error_arena(first_error)
                                sel.unregister(child_r)
                                continue
                            if key.data in ("stdout","stderr"):
                                raw=os.read(key.fd,65536)
                                if not raw:sel.unregister(key.fd);continue
                                target=self.capture_out if key.data=="stdout" else self.capture_err
                                if len(target)+len(raw)>INPUT_MAX:raise Refused("actor_stream_cap","execution")
                                target.extend(raw);self.observer.terminal_hold.commit(output=len(raw))
                            elif key.data=="terminal":
                                terminal=True
                            else:
                                self.drain_actor_streams()
                                self.verify_actual_actor_image()
                                request=self.frame.receive(force_prepaid=first_error is not None)
                                try:
                                    if first_error is not None and request['type'] not in ('actor-owner-state','bootstrap-owner-prefix','owner-body-begin','owner-body-chunk','owner-body-end','release','cleanup','failure','commit'):
                                        raise first_error
                                    reply={"ok":True,"value":self._control(request)}
                                except BaseException as exc:
                                    self.observer.retain_error_arena(exc)
                                    if first_error is None:
                                        first_error=exc
                                    reply={"ok":False,"error":error_fact(exc,"execution",self.frame.delivered)}
                                if self.frame.last_prepaid or first_error is not None:self.frame.send_prepaid(reply)
                                else:self.frame.send(reply)
                self.drain_actor_streams()
                status,usage,reap_faults=bounded_direct_reap(self.observer,pid,self.actor_pidfd)
                self.observer.cleanup_faults.extend(reap_faults)
                self.actor_status,self.actor_usage=status,usage
                if first_error is not None:raise first_error
                if os.waitstatus_to_exitcode(status)!=0:raise Refused("actor_terminal","terminal")
            finally:
                faults=self.cleanup_actor(pid,out_r,err_r)
                if faults:self.observer.cleanup_faults.extend(faults)
                if self.actor_status is not None:self.fdjournal.close_one(bundle_fd)
                else:
                    for lease in self.actor_exec_leases:lease.retain_until_terminal(pid)
        return self.finish()

    def drain_actor_streams(self):
        for fd,target in list(self.capture_fds.items()):
            while True:
                try:raw=os.read(fd,65536)
                except BlockingIOError:break
                except OSError as exc:
                    self.errors.append(error_fact(exc,"terminal"));break
                if not raw:break
                if len(target)+len(raw)>INPUT_MAX:raise Refused("actor_stream_cap","execution")
                target.extend(raw);self.observer.terminal_hold.commit(output=len(raw))

    def verify_actual_actor_image(self):
        if self.actor_exe_fact is not None:return
        pin=self.admission["image"]["launcher"]
        fd=self.fdjournal.acquire(os.open,"/proc/"+str(self.actor_pid)+"/exe",os.O_RDONLY|os.O_CLOEXEC,holder="actor-exe",credit=self.actor_graph_hold)
        try:
            s=os.fstat(fd)
            if identity9(s)!=pin["identity9_decimal_strings"]:raise Refused("actual_actor_executable_identity")
            hold=self.observer.reserve("actual_actor_executable_hash",reads=s.st_size,hash_bytes=s.st_size,allocation=65536)
            import hashlib
            try:
                h=hashlib.sha256();at=0
                while at<s.st_size:
                    raw=os.pread(fd,min(65536,s.st_size-at),at)
                    if not raw:raise Refused("actual_actor_executable_short")
                    hold.commit(reads=len(raw));hold.commit(hash_bytes=len(raw));h.update(raw);at+=len(raw)
                if h.hexdigest()!=pin["sha256"] or identity9(os.fstat(fd))!=identity9(s):raise Refused("actual_actor_executable_sha")
                self.actor_exe_fact={"pid":self.actor_pid,"sha256":h.hexdigest(),"identity9":identity9(s)}
            finally:hold.release()
        finally:self._close_temp(fd)

    def cleanup_actor(self,pid,out_r=-1,err_r=-1):
        if self.actor_fork_owner is not None:
            from lifetime import receive_preowned_fork_failure
            packet=receive_preowned_fork_failure(self.actor_fork_owner,pid)
            if packet is not None:self.bootstrap_prefix_graph=packet
        faults=[]
        if self.cleanup_attempted:return faults
        self.cleanup_attempted=True
        if self.actor_status is None:
            try:signal.pidfd_send_signal(self.actor_pidfd,signal.SIGKILL,None,0)
            except ProcessLookupError:pass
            except BaseException as exc:faults.append(error_fact(exc,"terminal"))
            try:
                if self.actor_pidfd<0:self.actor_pidfd=self.fdjournal.acquire(os.pidfd_open,pid,0,credit=self.actor_graph_hold,holder="actor-pidfd")
                status,usage,reap_faults=bounded_direct_reap(self.observer,pid,self.actor_pidfd,kill=True)
                self.actor_status,self.actor_usage=status,usage
                faults.extend(reap_faults)
            except BaseException as exc:faults.append(error_fact(exc,"terminal"))
        try:self.drain_actor_streams()
        except BaseException as exc:faults.append(error_fact(exc,"terminal"))
        if self.actor_status is None:
            self.observer.pidfds[pid]=self.actor_pidfd
            for lease in self.actor_exec_leases:lease.retain_until_terminal(pid)
            faults.append({"cause":"STOP_UNCONFIRMED","pid":pid,"pidfd_retained":True,
                "full_exec_input_pipe_FD_reservation_graph_retained":True})
            return faults
        for lease in self.actor_exec_leases:faults.extend(lease.retire(confirmed=True))
        for fd in self.capture_fds:self.fdjournal.close_one(fd)
        self.capture_fds={}
        self.fdjournal.close_one(self.actor_pidfd);self.observer.pidfds.pop(pid,None)
        # All performer reservations retire without re-entering the exhausted
        # budget. Parent-owned native and terminal reservations remain owned.
        if self.actor_ownership_complete:
            for token,row in list(self.observer.pending.items()):
                if row.get('owner_pid')==pid:self.observer.release(token)
        if self.actor_fork_owner is not None and self.actor_fork_hold is not None:
            try:self.actor_fork_owner.record_parent_table_end(pid,self.actor_status)
            except BaseException as end_error:
                self.observer.retain_error_arena(end_error)
                faults.append(error_fact(end_error,"terminal"))
            else:
                # The exact helper already cancelled only this prospective
                # Root copy. Failed/partial cancellation keeps all FD credit.
                self.actor_fork_hold.retire_slots()
            if self.actor_fork_graph is not None:
                self.observer.retain_allocation(self.actor_fork_hold.token,self.actor_fork_graph['_receiver_resident_upper']+128*8192)
        if self.frame is not None:
            endpoints=(self.frame.incoming,self.frame.outgoing)
            faults.extend(self.frame.close())
        self.actor_local_graph={"status":"OWNER_TERMINATED_WAIT4_CONFIRMED","pid":pid,
            "raw_wait_status":self.actor_status,"last_actor_journal":self.actor_local_graph,
            'actual_body_acceptance_complete':self.actor_ownership_complete,
            'bootstrap_prefix_graph':self.bootstrap_prefix_graph}
        self.actor_pidfd=-1
        # Pipe/bundle journals may still contain a failed-close descriptor.
        # Their Root lifetime credit retires only at the complete close boundary.
        return faults

    def cleanup_owned_scope(self):
        # Only the independently enrolled, initially empty cgroup belongs to
        # this invocation. No PID search, process-group kill or host-wide kill.
        if self.scope_cleanup_attempted or self.observer is None:return
        self.scope_cleanup_attempted=True
        try:pids=self.observer.sample()["processes"]
        except BaseException as exc:
            self.observer.note_cleanup(error_fact(exc,"terminal"))
            # Unknown scope enumeration never fabricates an empty set. Still
            # stop every exact previously acquired and owned pidfd.
            pids=list(self.observer.pidfds)
        owned=[]
        for pid in pids:
            if pid==self.actor_pid:continue
            fd=-1;previously_owned=pid in self.observer.pidfds
            try:
                fd=self.observer.pidfds.get(pid,-1)
                if fd<0:
                    # The validation grant remains live through error cleanup;
                    # acquiring a missing exact pidfd uses that preowned slot.
                    fd=self.fdjournal.acquire(os.pidfd_open,pid,0,holder="scope-pidfd")
                if pid not in self.observer.processes:self.observer.register(pid,fd)
                else:self.observer.pidfds[pid]=fd
                # Actual IO is retained before kill/reap even for an unexpected
                # descendant. Unknown IO stops proof; it is not replaced by 0.
                owned.append((pid,fd))
            except BaseException as exc:
                if fd>=0:self.observer.pidfds[pid]=fd
                self.observer.note_cleanup(error_fact(exc,"terminal"))
                if fd>=0 and (previously_owned or pid in self.observer.processes):owned.append((pid,fd))
            # Observation failure and cleanup authorization are different
            # domains. An IO failure cannot skip exact already-owned stopping.
            if fd>=0 and (pid,fd) in owned:
                try:self.observer.before_reap(pid)
                except BaseException as exc:self.observer.note_cleanup(error_fact(exc,"terminal"))
                try:signal.pidfd_send_signal(fd,signal.SIGKILL,None,0)
                except ProcessLookupError:pass
                except BaseException as exc:self.observer.note_cleanup(error_fact(exc,"terminal"))
        for pid,fd in owned:
            try:
                status,usage,faults=bounded_direct_reap(self.observer,pid,fd,kill=False)
                self.observer.cleanup_faults.extend(faults)
                if status is None:
                    self.observer.note_cleanup({"cause":"STOP_UNCONFIRMED","pid":pid,"pidfd_retained":True})
                else:
                    if self.native is not None and pid in self.native.unconfirmed:self.native.retire_confirmed(pid,status)
                    else:
                        if fd not in self.fdjournal.meta:
                            self.observer.note_cleanup({"cause":"FD_OWNER_JOURNAL_UNKNOWN","fd":fd,"pid":pid})
                        else:
                            self.fdjournal.close_one(fd)
                            if fd not in self.fdjournal.fds:self.observer.pidfds.pop(pid,None)
            except BaseException as exc:self.observer.note_cleanup(error_fact(exc,"terminal"))

    def finish(self):
        # Postdelivery/full terminal includes all actual streams, direct wait4,
        # cleanup, current final source pins and the independent actual meter.
        if self.finished is not None:return self.finished
        if self.actor_pid>0 and self.actor_status is None:
            self.observer.cleanup_faults.extend(self.cleanup_actor(self.actor_pid))
        self.cleanup_owned_scope()
        out=self.store.put_reserved("whole-actor-stdout",bytes(self.capture_out),"native",None,self.observer.terminal_hold)
        err=self.store.put_reserved("whole-actor-stderr",bytes(self.capture_err),"native",None,self.observer.terminal_hold)
        # Actual FD-journal/consumer-loader/store cleanup happens BEFORE this
        # terminal is encoded. The separately acquired forward FD is explicitly
        # transferred to the same existing Root delivery owner, not claimed shut.
        self.close(retain_forward=True)
        handoff=self.existing_owner_handoff()
        try:outer=self.observer.receipt(final=True)
        except BaseException as exc:
            try:last_kernel=self.observer.sample()
            except BaseException as sample_error:last_kernel={"status":"UNKNOWN_NOT_ZERO_NOT_PROVEN","error":error_fact(sample_error,"terminal")}
            outer={"status":"UNKNOWN_NOT_ZERO_NOT_PROVEN","error":error_fact(exc,"terminal"),
                "last_raw_kernel":last_kernel,"wait4":self.observer.waits,
                "cleanup_faults":self.observer.cleanup_faults,"pending":self.observer.reservation_graph()}
        terminal={"schema":"friday.a138.root-owned-whole-terminal.v1","root_fact":self.root_fact,
            "launch":self.actual_launch_fact,"qualification":self.qualification,
            "stdout":out,"stderr":err,"actor_wait_status":self.actor_status,
            "native_invocations":self.native.invocations if self.native else [],"errors":self.errors,
            "retained_actual_receipts":self.retained_receipts,
            "independent_selector_receipts":self.selector_receipts,
            "independent_expected_not_created":True,"launch_mode":self.admission["launch_mode"] if self.admission else None,
            "full_consumer_output_pin":self.full_output_pin,"full_five_argument_golden_binding":self.final_binding,
            "produced_public_preimage_pin":self.final_public_pin,
            "actual_owner_cleanup_boundary":{"ordinary_close_attempt_completed":self.owner_closed,
                "cleanup_faults":list(self.observer.cleanup_faults),"delivery_owner_handoff":handoff},
            "outer":outer,"source_issued_grant":False,
            "effects_granted":False,"GO":False}
        raw=canonical(terminal,None,INPUT_MAX)
        self.observer.terminal_hold.commit(reads=len(raw)*2)
        # Terminal forward hold was acquired before any actor/native effect.
        # Its actual bytes are charged without re-entering an exhausted meter.
        pin=self.write_forward_terminal(raw)
        self.fdjournal.close_one(self.forward_fd)
        forward_closed=self.forward_fd not in self.fdjournal.fds
        if forward_closed:self.forward_fd=-1
        self.observer.cleanup_faults.extend(self.fdjournal.faults)
        try:after=self.observer.receipt(final=True)
        except BaseException as exc:after={"status":"UNKNOWN_NOT_ZERO_NOT_PROVEN","error":error_fact(exc,"terminal")}
        tail={"schema":"friday.sol053.actual-post-terminal-cleanup-tail.v1","terminal_pin":pin,
            "terminal_forward_close_confirmed":forward_closed,"full_consumer_output_pin":self.full_output_pin,
            "full_five_argument_golden_binding":self.final_binding,
            "actual_cleanup_faults":list(self.observer.cleanup_faults),
            "post_terminal_observation":after,"actual_held_handoff":self.existing_owner_handoff(),
            "finite_retirement_api":"retire_existing_root_delivery","ownership_retired":False,"GO":False}
        tail_raw=canonical(tail,None,INPUT_MAX)
        self.observer.terminal_hold.commit(reads=len(tail_raw)*2)
        tail_pin=self.write_cleanup_tail(tail_raw)
        self.delivery_pins=(pin,tail_pin)
        self.finished={"terminal_pin":pin,"cleanup_tail_pin":tail_pin,"independently_observed_postdelivery":after,
                "existing_Root_owner_key":handoff["owner_key"],"ownership_retired":False,
                "finite_retirement_api":"retire_existing_root_delivery",
                "publisher_proof":False,"effects_granted":False,"GO":False}
        return self.finished

    def write_cleanup_tail(self,raw):
        if len(raw)>INPUT_MAX or self.tail_fd<0:raise Refused("cleanup_tail_capacity","terminal")
        at=0
        while at<len(raw):
            n=os.write(self.tail_fd,memoryview(raw)[at:at+65536])
            if n<=0:raise Refused("cleanup_tail_short","terminal")
            at+=n;self.observer.terminal_hold.commit(output=n)
        os.fsync(self.tail_fd);s=os.fstat(self.tail_fd)
        return {"path":self.store.root+"/whole-cleanup-tail","bytes":len(raw),"sha256":sha(raw,self.observer,self.observer.terminal_hold),
            "identity9_decimal_strings":identity9(s)}

    def write_forward_terminal(self,raw):
        if len(raw)>INPUT_MAX:raise Refused("terminal_capacity","terminal")
        fd=self.forward_fd
        if fd<0:raise Refused("durable_forward_endpoint_missing","terminal")
        at=0
        try:
            while at<len(raw):
                n=os.write(fd,memoryview(raw)[at:at+65536])
                if n<=0:raise Refused("terminal_short","terminal")
                at+=n;self.observer.terminal_hold.commit(output=n)
            os.fsync(fd);s=os.fstat(fd)
            return {"path":self.store.root+"/whole-terminal","bytes":len(raw),"sha256":sha(raw,self.observer,self.observer.terminal_hold),
                    "identity9_decimal_strings":identity9(s)}
        finally:
            # Deliberate live lease. Its same-owner durable handoff, complete
            # pending graph and actual close errors cannot be hidden as cleanup.
            pass

    def existing_owner_handoff(self):
        pid=self.observer.owner_pid if self.observer is not None else self.partial_owner_pid
        if self.observer is not None and self.enrollment_pin is not None:
            key=(pid,self.enrollment_pin["sha256"],self.entry_ns)
        else:
            key=(pid,self.entry_ns)
        partial=(self.partial_owner_pid,self.entry_ns)
        for owner_key in (partial, key):
            if owner_key in RETAINED_ROOT_OWNERS and RETAINED_ROOT_OWNERS[owner_key] is not self:
                raise Refused("Root_owner_handoff_collision","terminal")
            RETAINED_ROOT_OWNERS[owner_key]=self
            self.owner_keys.add(owner_key)
        fds=[]
        for fd in sorted(self.fdjournal.fds):
            meta=dict(self.fdjournal.meta.get(fd,{}))
            try:
                fact=dict(meta)
                if meta.get("status")!="UNKNOWN":fact["current_identity9"]=identity9(os.fstat(fd))
            except BaseException as exc:fact={"fd":fd,"status":meta.get("status","UNKNOWN"),"holder":meta.get("holder"),"credit":meta.get("credit"),"identity9":meta.get("identity9_decimal_strings"),"error":error_fact(exc,"terminal")}
            fds.append(fact)
        native=self.native.ownership() if self.native is not None else []
        return {"owner_key":[str(v) for v in key],"same_existing_Root_pid":pid,
            "root_start_ticks":self.root_fact["start_ticks"] if self.root_fact else None,
            "actor_status":self.actor_status,"actor_held_exec_pins":[{"fd":p.fd,"path":p.path,"expected_pin":p.pin} for p in self.actor_exec_leases if p.fd>=0],
            "fd_graph":fds,"native_graph":native,
            "complete_root_local_graph":self.observer.local_owner_graph() if self.observer is not None else [],
            "actor_local_graph":self.actor_local_graph,
            "actor_pre_exec_graph":self.actor_fork_graph,
            "complete_root_result_graph":self.observer.result_owner_graph() if self.observer is not None else [],
            "accepted_native_capture_graph":[capture.get('accepted_receipt') for metadata,capture,hold in getattr(self.observer,'accepted_captures',{}).values()] if self.observer is not None else [],
            "final_caller_arena":self.final_arena.capture(),
            "preobserver_journals":[j.graph() for j in self.pre_hash.journals] if hasattr(self,"pre_hash") else [],
            "retained_journals":[j.graph() for j in self.retained_journals],
            "validation_arena_tokens":[h.token for h in self.validation_holds],
            "owner_registrations":[[str(v) for v in k] for k in self.owner_keys],
            "held_file_lease_graph":self.observer.held_lease_graph() if self.observer is not None else [],
            "reservation_graph":self.observer.reservation_graph() if self.observer is not None else [],
            "status":"STOP_UNCONFIRMED" if (self.actor_pid>0 and self.actor_status is None) or native else "ORDINARY_OWNER_CLOSED_DELIVERY_LEASE_HELD" if self.owner_closed else "FD_CLOSE_OR_OWNER_UNCONFIRMED",
            "store_FD_graph":{"root_fd":self.store.root_fd if self.store else -1,
                "pending_file_fds":sorted(self.store.pending_fds) if self.store else [],
                "journal":[dict(self.store.fdjournal.meta[fd]) for fd in sorted(self.store.fdjournal.fds)] if self.store is not None and hasattr(self.store,"fdjournal") else []},
            "delivery_fd":self.forward_fd,"cleanup_tail_fd":self.tail_fd,
            "same_owner_transfer_validated":True,"GO":False}

    def close(self,retain_forward=False,completion=None):
        if self.owner_closed:return
        if self.actor_pid>0 and self.actor_status is None and self.observer is not None:
            self.observer.cleanup_faults.extend(self.cleanup_actor(self.actor_pid))
        self.cleanup_owned_scope()
        if (self.actor_pid>0 and self.actor_status is None) or (self.native and (self.native.unconfirmed or getattr(self.native,"capture_owners",{}))):
            # Entire parent loader/store/bundle/frame/FD and allocation graph
            # stays alive. The persistent SAME Root owner, not a new service,
            # receives it through the durable terminal handoff.
            return
        retained={fd for fd in (self.forward_fd,self.tail_fd) if fd>=0} if retain_forward else set()
        for journal in self.retained_journals:
            journal.close()
        faults=self.fdjournal.close(exclude=retained)
        if self.observer is not None:self.observer.cleanup_faults.extend(faults)
        else:self.errors.extend(faults)
        self.phase_ordinary=None
        for owned in (self.loader,self.store):
            if owned is not None:
                try:
                    if owned is self.store:owned.close(completion)
                    else:owned.close()
                except BaseException as exc:
                    fact=error_fact(exc,"terminal")
                    if self.observer is not None:self.observer.note_cleanup(fact)
                    else:self.errors.append(fact)
        if self.observer is not None:
            for sequence,owned in list(self.observer.local_owners.values()):
                if owned is self.fdjournal or owned is self.store:continue
                if isinstance(owned,PreparedFullBody):
                    if completion is None:continue
                    try:owned.transfer_and_close(completion.receipt)
                    except BaseException as exc:
                        completion.receipt.receiver.retain_after_document(exc,'Source-full-body-close')
                        self.observer.retain_error_arena(exc)
                    continue
                try:
                    owned.close()
                    self.observer.retire_local_owner(owned)
                except BaseException as exc:self.observer.note_cleanup(error_fact(exc,"terminal"))
        if self.actor_pid<=0 or self.actor_status is not None:
            for lease in self.actor_exec_leases:
                if self.observer is None:
                    self.errors.append({"cause":"lease_retire_without_observer","fd":lease.fd,"path":lease.path,"status":"UNKNOWN"})
                else:
                    self.observer.cleanup_faults.extend(lease.retire(confirmed=True))
            if self.observer is not None and not self.fdjournal.fds-retained:
                if self.bundle_hold is not None and self.bundle_hold.release() is True:self.bundle_hold=None
                if self.actor_graph_hold is not None and self.actor_graph_hold.release() is True:self.actor_graph_hold=None
        held=self.observer.held_leases if self.observer is not None else {}
        store_open=False
        if self.store is not None:
            journal_left=self.store.fdjournal.fds if hasattr(self.store,"fdjournal") else set()
            store_open=self.store.root_fd>=0 or bool(self.store.pending_fds) or bool(journal_left)
        local_pending=any((getattr(owned,"fdjournal",owned if isinstance(owned,OwnedFDs) else None) is not None and
            bool(getattr(owned,"fdjournal",owned if isinstance(owned,OwnedFDs) else None).fds))
            for sequence,owned in self.observer.local_owners.values() if owned is not self.fdjournal) if self.observer else False
        stock_pending=any(j.fds for j in self.pre_hash.journals) if hasattr(self,"pre_hash") else False
        self.owner_closed=not (self.fdjournal.fds-retained) and not held and not any(p.fd>=0 for p in self.actor_exec_leases) and not store_open and not local_pending and not stock_pending and not any(j.fds for j in self.retained_journals)
        if not retain_forward and self.owner_closed and self.observer is not None and not self.observer.pidfds:
            if hasattr(self,"forward_hold"):self.forward_hold.release()
            if hasattr(self.observer,"terminal_hold"):self.observer.terminal_hold.release()

    def retire_delivery(self,owner_key,terminal_pin,tail_pin,completion=None):
        """One finite SAME-Root call AFTER caller durably retained the result.

        The caller must have dropped all escaping Source/consumer/body aliases.
        No new daemon/owner, polling, retry, authority grant or optimistic free.
        The caller retains the returned actual close outcome in its own durable
        native-tool completion. If any graph is unconfirmed, all stays held.
        """
        self.observer._owned()
        if completion is not self.final_arena:raise Refused('actual_final_arena_required','terminal')
        completion.require_complete(self)
        if self.delivery_retire_attempted:raise Refused("delivery_retirement_already_attempted","terminal")
        key=(self.observer.owner_pid,self.enrollment_pin["sha256"],self.entry_ns)
        if owner_key!=[str(v) for v in key] or RETAINED_ROOT_OWNERS.get(key) is not self or self.delivery_pins!=(terminal_pin,tail_pin):
            raise Refused("complete_existing_Root_delivery_identity","terminal")
        if self.root_fact and proc_start(os.getpid(),self.observer)["start_ticks"]!=self.root_fact["start_ticks"]:
            raise Refused("existing_Root_process_reused","terminal")
        # Read/hash with ALREADY acquired terminal credit; do not re-enter an
        # exhausted or expired execution meter to retire ownership.
        import hashlib
        for pin in (terminal_pin,tail_pin):
            fd=open_absolute(pin["path"],journal=self.fdjournal)
            try:
                if identity9(os.fstat(fd))!=pin["identity9_decimal_strings"]:raise Refused("durable_delivery_drift","terminal")
                h=hashlib.sha256();at=0
                while at<pin["bytes"]:
                    raw=os.pread(fd,min(65536,pin["bytes"]-at),at)
                    if not raw:raise Refused("durable_delivery_short","terminal")
                    at+=len(raw);self.observer.terminal_hold.commit(hash_bytes=len(raw));h.update(raw);self.observer.terminal_hold.commit(reads=len(raw))
                if h.hexdigest()!=pin["sha256"] or identity9(os.fstat(fd))!=pin["identity9_decimal_strings"] or identity9(os.lstat(pin["path"]))!=pin["identity9_decimal_strings"]:
                    raise Refused("durable_delivery_full_pin","terminal")
            finally:self.fdjournal.close_one(fd)
        self.delivery_retire_attempted=True
        self.close(retain_forward=True,completion=completion)
        if not self.owner_closed or self.observer.pidfds or self.observer.held_leases or self.native and (self.native.unconfirmed or getattr(self.native,"capture_owners",{})) or self.fdjournal.fds-{self.tail_fd} or any(j.fds for j in self.retained_journals) or any(j.fds for j in self.pre_hash.journals):
            return {"status":"STOP_UNCONFIRMED","actual_held_handoff":self.existing_owner_handoff(),"ownership_retired":False,"GO":False}
        self.fdjournal.close_one(self.tail_fd)
        if self.tail_fd in self.fdjournal.fds:
            self.observer.cleanup_faults.extend(self.fdjournal.faults)
            return {"status":"FD_CLOSE_UNCONFIRMED","actual_held_handoff":self.existing_owner_handoff(),"ownership_retired":False,"GO":False}
        self.tail_fd=-1
        self.observer.cleanup_faults.extend(self.fdjournal.faults)
        faults=list(self.observer.cleanup_faults)
        root_fact=self.root_fact
        if not self._settle_complete_source_graph(completion):
            return {'status':'CREDIT_HISTORY_OR_ALIAS_COMPLETION_UNCONFIRMED',
                'actual_held_handoff':self.existing_owner_handoff(),'ownership_retired':False,'GO':False}
        return {'schema':'friday.a190.actual-existing-root-delivery-completion.v1',
            'status':'ACTUAL_DELIVERY_FD_CLOSE_AND_COMPLETE_SOURCE_RETIREMENT_CONFIRMED',
            'ownership_retired':True,'owner_key':owner_key,'root_fact':root_fact,
            'terminal_pin':terminal_pin,'cleanup_tail_pin':tail_pin,
            'actual_final_close_faults':faults,'caller_must_durably_retain_this_actual_completion':True,'GO':False}

    def _settle_complete_source_graph(self,completion):
        """One same-Source end transaction, after actual full receiver receipt.

        Retain all roots on any unresolved row or failed credit release. No
        registration/history is erased just because OS child termination was
        confirmed; settlement covers all actually attached Source aliases.
        """
        from observer import Reservation
        from common import PreObserverHash
        from lifetime import _OCCUPIED
        completion.require_complete(self)
        completion.receipt.receiver.require_transfer(completion.receipt)
        if self.actor_pid>0 and not self.actor_ownership_complete:return False
        receiver=completion.receipt.receiver
        seen=set();todo=[self];grants={};books=[];states=[]
        modules={'root_tool_adapter','observer','common','lifetime','custody','consumer_bridge',
            'native','normalization','extraction','independent_selector','actor_context','owned_prefix_bank'}
        while todo:
            value=todo.pop();key=id(value)
            if key in seen:continue
            seen.add(key)
            if value is completion or value is receiver:continue
            if len(seen)>262144:return False
            if isinstance(value,(Reservation,PreObserverHash)):
                prior=grants.get(value.token)
                if prior is not None and prior is not value and not prior.closed and not value.closed:return False
                if prior is None or prior.closed:grants[value.token]=value
            if isinstance(value,OwnedFDs) or isinstance(value,ForkOwner):books.append(value)
            if type(value).__name__=='FDState' and type(value).__module__=='lifetime':states.append(value)
            if type(value) is dict:todo.extend(value.keys());todo.extend(value.values())
            elif type(value) in (list,tuple,set):todo.extend(value)
            elif type(value).__module__ in modules:todo.extend(value.__dict__.values())
        for book in books:
            if any(row.get('journal') is book and row['status'] in _OCCUPIED for row in book.rows):return False
        if any(getattr(hold,'fd_rows',{}) for hold in grants.values()):return False
        observer=self.observer
        if observer is not None and any(token not in grants for token in observer.pending):return False
        for registered in self.owner_keys:
            if RETAINED_ROOT_OWNERS.get(registered) is not self:return False
        # No clearing occurs before every exact actual credit is confirmed.
        # A failed release remains visible with the surviving full owner tree.
        if observer is not None:observer.fd_history_completion_received=True
        for hold in grants.values():
            if hold.release() is not True:return False
        if observer is not None and (observer.pending or observer.live_alloc or observer.live_slots):return False
        for state in states:
            if not state.retire_confirmed(completion):return False
        for book in books:
            if book.rows and not book.retire_history(completion):return False
        # Explicitly detach every known cross-root pointer and then clear the
        # complete adapter namespace, including fork/export holds and aliases.
        for hold in grants.values():
            if isinstance(hold,Reservation):hold.meter=None
            elif isinstance(hold,PreObserverHash):
                hold.observer=None;hold.journals.clear();hold.fd_state=None
        if observer is not None:
            observer.__dict__.clear()
        keys=list(self.owner_keys)
        receiver.capture('actual-pre-Source-detach',[self,observer])
        completion.detach_confirmed()
        self.__dict__.clear()
        for registered in keys:
            if RETAINED_ROOT_OWNERS.get(registered) is self:RETAINED_ROOT_OWNERS.pop(registered)
        self.delivery_retire_attempted=True
        return True

    def retire_prefix(self,owner_key,completion=None):
        """Finite same-Root retirement of a confirmed preparation refusal.

        The actual native caller already persisted the returned refusal and
        dropped its Source/error aliases. No delivery pin is invented for a
        prefix that never produced one. Any exact unresolved graph stays owned.
        """
        if os.getpid()!=self.partial_owner_pid:raise Refused("observer_owner","terminal")
        if completion is not self.final_arena:raise Refused('actual_final_arena_required','terminal')
        completion.require_complete(self)
        matching=[k for k in self.owner_keys if [str(v) for v in k]==owner_key]
        if len(matching)!=1 or RETAINED_ROOT_OWNERS.get(matching[0]) is not self:
            raise Refused("existing_Root_prefix_identity","terminal")
        if self.delivery_pins is not None or self.actual_launch_fact is not None or self.actor_pid>0 or self.native and self.native.invocations:
            raise Refused("prefix_requires_delivery_retirement","terminal")
        if self.delivery_retire_attempted:raise Refused("delivery_retirement_already_attempted","terminal")
        self.delivery_retire_attempted=True
        self.close(completion=completion)
        if not self.owner_closed or self.fdjournal.fds or any(j.fds for j in self.retained_journals) or hasattr(self,"pre_hash") and any(j.fds for j in self.pre_hash.journals):
            return {"ownership_retired":False,"status":"FD_CLOSE_OR_OWNER_UNCONFIRMED",
                "actual_held_handoff":self.existing_owner_handoff(),"GO":False}
        if self.observer is not None and (self.observer.pidfds or self.observer.held_leases):
            return {"ownership_retired":False,"status":"STOP_UNCONFIRMED", "GO":False}
        for registered in self.owner_keys:
            if RETAINED_ROOT_OWNERS.get(registered) is not self:raise Refused("Root_owner_retirement_collision","terminal")
        faults=list(self.errors)
        if self.observer is not None:faults.extend(self.observer.cleanup_faults)
        if not self._settle_complete_source_graph(completion):
            return {'ownership_retired':False,'status':'CREDIT_HISTORY_OR_ALIAS_COMPLETION_UNCONFIRMED',
                'actual_held_handoff':self.existing_owner_handoff(),'GO':False}
        return {'status':'ACTUAL_PREFIX_COMPLETE_SOURCE_RETIREMENT_CONFIRMED','ownership_retired':True,
            'owner_key':owner_key,'actual_final_close_faults':faults,'GO':False}


def retire_existing_root_delivery(owner_key,terminal_pin,tail_pin,completion=None):
    """Explicit existing-tool delivery completion, never a background loop."""
    matches=[owner for key,owner in RETAINED_ROOT_OWNERS.items() if [str(v) for v in key]==owner_key]
    if len(matches)!=1:raise Refused("existing_Root_delivery_owner","terminal")
    return matches[0].retire_delivery(owner_key,terminal_pin,tail_pin,completion)


def retire_existing_root_prefix(owner_key,completion=None):
    matches=[owner for key,owner in RETAINED_ROOT_OWNERS.items() if [str(v) for v in key]==owner_key]
    if len(matches)!=1:raise Refused("existing_Root_prefix_owner","terminal")
    return matches[0].retire_prefix(owner_key,completion)

def consume_existing_root_final_arena(owner_key,consumer):
    matches=[owner for key,owner in RETAINED_ROOT_OWNERS.items() if [str(v) for v in key]==owner_key]
    if len(matches)!=1:raise Refused('existing_Root_final_arena_owner','terminal')
    arena=matches[0].final_arena;arena.consume(consumer)
    return arena


def _perform_independently_invoked_root_tool(case_id,caller):
    """Real performing entry; externally enrolled existing Root tool calls it."""
    adapter=None
    closed_for_fallback=False
    try:
        # The real caller retains the partially initialized instance too.
        adapter=RootToolAdapter.__new__(RootToolAdapter)
        caller.owner=adapter
        caller.retain_before_birth(adapter,'actual-RootToolAdapter-before-init')
        adapter.prefix_error_arenas=[];adapter.errors=[];adapter.observer=None
        adapter.store=None;adapter.frame=None;adapter.forward_fd=-1
        adapter.finished=None;adapter.native=None;adapter.actual_launch_fact=None
        adapter.retained_journals=[];adapter.fdjournal=OwnedFDs()
        adapter.__init__()
        adapter.prepare()
        return adapter.launch(case_id)
    except BaseException as exc:
        if adapter is None:
            return {"wire":bounded_refusal(getattr(exc,"cause","exception"),"before-effect",0,True),
                "original_error":error_fact(exc,"before-effect"),"GO":False}
        # A failed RootObserver constructor is already retained, but its
        # result arena may not exist yet. Do not hide the primary by calling
        # its incomplete error-retention interface.
        if not hasattr(adapter,'prefix_error_arenas'):adapter.prefix_error_arenas=[]
        adapter.prefix_error_arenas.append(exc)
        if getattr(adapter,'observer',None) is not None:
            try:adapter.observer.retain_error_arena(exc)
            except BaseException as retention_error:
                adapter.prefix_error_arenas.append(retention_error)
        journal=getattr(exc,"retained_journal",None)
        if journal is not None:
            adapter.retained_journals.append(journal)
            try:
                for fd in tuple(journal.fds):journal.transfer(fd,adapter.fdjournal)
            except BaseException as transfer_error:
                adapter.prefix_error_arenas.append(transfer_error)
                adapter.errors.append(error_fact(transfer_error,"terminal"))
        adapter.errors.append(error_fact(exc,"terminal",adapter.frame.delivered if adapter.frame else 0))
        try:
            if adapter.store is None or adapter.observer is None or adapter.forward_fd<0:
                raise Refused("preparation_failed","before-effect")
            return adapter.finish()
        except BaseException as terminal_error:
            adapter.prefix_error_arenas.append(terminal_error)
            try:
                adapter.close()
            except BaseException as close_error:
                adapter.prefix_error_arenas.append(close_error)
                adapter.errors.append(error_fact(close_error,"terminal"))
            closed_for_fallback=True
            try:
                handoff=adapter.existing_owner_handoff()
            except BaseException as handoff_error:
                adapter.prefix_error_arenas.append(handoff_error)
                handoff={"status":"UNKNOWN_NOT_ZERO_NOT_PROVEN","error":error_fact(handoff_error,"terminal")}
            journal_unknown=bool(adapter.fdjournal.fds) or any(row.get("status")=="UNKNOWN" for row in adapter.fdjournal.meta.values())
            cleanup_faults=getattr(adapter.observer,'cleanup_faults',None)
            cleanup_ok=(adapter.observer is not None and type(cleanup_faults) is list
                and not journal_unknown and not cleanup_faults)
            fd_meta=[{"fd":fd,"holder":adapter.fdjournal.meta.get(fd,{}).get("holder"),
                "credit":adapter.fdjournal.meta.get(fd,{}).get("credit"),
                "identity9_decimal_strings":adapter.fdjournal.meta.get(fd,{}).get("identity9_decimal_strings"),
                "status":adapter.fdjournal.meta.get(fd,{}).get("status")} for fd in sorted(set(adapter.fdjournal.fds)|{fd for fd,row in adapter.fdjournal.meta.items() if row.get("status")=="UNKNOWN"})]
            return {"wire":bounded_refusal(getattr(exc,"cause","exception"),"terminal",
                    adapter.frame.delivered if adapter.frame else 0,cleanup_ok),
                "original_error":error_fact(exc,"terminal"),"terminal_error":error_fact(terminal_error,"terminal"),
                "errors":list(adapter.errors),"fd_journal":fd_meta,"faults":list(adapter.fdjournal.faults),
                "actual_held_handoff":handoff,"ownership_retired":False,
                "effects_possible":bool((adapter.native and adapter.native.invocations) or adapter.actual_launch_fact),
                "actual_RAM_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN","GO":False}
    finally:
        if adapter is not None and not closed_for_fallback and adapter.finished is None and adapter.forward_fd<0:
            adapter.close()


def independently_invoked_root_tool(case_id):
    """Actual native-enrolled performing entry, not after-lifecycle wrapper."""
    from existing_root_caller import current_native_owner
    caller=current_native_owner();result=None
    try:
        result=_perform_independently_invoked_root_tool(case_id,caller)
        owner=caller.owner
        if owner is None:raise Refused('native_current_adapter_birth','terminal')
        caller.native.hold(caller.context,'actual-performing-return',result)
        if (getattr(owner,'qualification',None) is None or getattr(owner,'final_body_carrier',None) is None):
            primary=next(iter(reversed(getattr(owner,'prefix_error_arenas',()))),None)
            if primary is None:raise Refused('actual_prefix_error_missing','terminal')
            return {'Source_result':result,'actual_native_custody':caller.complete_prefix(owner,primary),
                'ownership_retired':False,'GO':False}
        handoff=owner.existing_owner_handoff()
        arena=consume_existing_root_final_arena(handoff['owner_key'],lambda roots:caller.consume_final(owner,roots))
        if owner.delivery_pins is None:end=owner.retire_prefix(handoff['owner_key'],arena)
        else:end=owner.retire_delivery(handoff['owner_key'],*owner.delivery_pins,completion=arena)
        caller.source_end_received(caller.receipt,end)
        outside=caller.finish_outside()
        return {'Source_result':result,'actual_Source_end':end,'actual_native_outside_end':outside,
            'ownership_retired':outside.get('ownership_retired') is True,'GO':False}
    except BaseException as error:
        caller.retain_error(error,'actual-performing-entry')
        try:caller.retain_after_document(error,'actual-performing-entry')
        except BaseException as capture_error:caller.retain_error(capture_error,'native-full-capture')
        return {'Source_result':result,'status':'NATIVE_ACTUAL_OWNER_HELD_STOP_UNCONFIRMED',
            'ownership_retired':False,'actual_original_error':error,
            'actual_RAM_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','GO':False}
