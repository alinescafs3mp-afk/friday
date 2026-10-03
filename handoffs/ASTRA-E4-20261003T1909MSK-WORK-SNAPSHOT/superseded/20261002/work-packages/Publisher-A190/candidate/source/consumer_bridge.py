"""Full physical input adapter, actual A128 calls, unchanged schema/cap limits."""
import importlib.abc
import importlib.util
import os
import sys
from common import (Refused, INPUT_MAX, DOCUMENT_MAX, OUTPUT_MAX, exact, canonical,
                    parse, sha, domain, integer, text, mono, KINDS)
from custody import Held


class HeldConsumerLoader(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    """Only future admitted imports: compile SAME held reviewed source bytes."""
    def __init__(self, pins, meter):
        self.pins={os.path.basename(r["relative_path"])[:-3]:r for r in pins
                   if r["relative_path"].startswith("source/") and r["relative_path"].endswith(".py")}
        self.meter=meter;self.bodies={};self.loaded=[];self.previous={}
    def acquire(self):
        self.residency=self.meter.reserve("consumer-source-residency",
            allocation=sum(r["bytes"] for r in self.pins.values())*8+65536)
        self.consumer_arena=self.meter.reserve("full-consumer-allocation-lifetime",allocation=0)
        for name,pin in self.pins.items():
            with Held(pin["path"],pin,self.meter,DOCUMENT_MAX,True) as held:
                self.bodies[name]=held.read(DOCUMENT_MAX)
        # Import bytes stay reachable for the complete owner lifetime.
        return self
    def find_spec(self,fullname,path=None,target=None):
        if fullname in self.bodies:return importlib.util.spec_from_loader(fullname,self,origin=self.pins[fullname]["path"])
        return None
    def create_module(self,spec):return None
    def exec_module(self,module):
        name=module.__name__
        raw=self.bodies[name]
        module.__file__=self.pins[name]["path"]
        # This SOURCE is not executed in A138. Future Root has already qualified
        # independent review, exact snapshot, interpreter/image and effect scope.
        code=compile(raw,module.__file__,"exec",dont_inherit=True)
        exec(code,module.__dict__)
        if name=="resource_meter":
            # Preserve every original inner gate/error. The Root-owned outer
            # ledger observes/admits logical hash/read and object allocations
            # prospectively in addition to real OS/cgroup observations.
            original_read=module.debit_read;original_allocate=module.reserve_allocation
            original_checkpoint=module.checkpoint;original_hash=module.debit_hash
            module.OUTER_STOCK_SAMPLE=self.meter.owned_sample
            module.OUTER_FD_SCOPE=self.meter.owned_file
            def outer_complete_call(roots):
                if roots['owner_pid']!=os.getpid():raise Refused('consumer_call_owner','terminal')
                # Existing same-Source observer owns complete actual values,
                # histories and original errors under its already-live arena.
                # No new ordinary reserve is requested on a refusal prefix.
                if self.consumer_arena.closed:raise Refused('consumer_call_credit_retired','terminal')
                self.meter.own_result(roots,self.consumer_arena)
                return {'owner_pid':os.getpid(),'call_sequence':roots['call_sequence'],'actual_roots_received':True}
            module.OUTER_COMPLETE_CALL=outer_complete_call
            def outer_read(size):
                hold=self.meter.reserve("full-consumer-physical-read",reads=size)
                try:
                    original_read(size)
                    hold.commit(reads=size)
                finally:hold.release()
            def outer_hash(size):
                hold=self.meter.reserve("full-consumer-memory-hash",hash_bytes=size,allocation=64)
                try:
                    original_hash(size)
                    hold.commit(hash_bytes=size)
                finally:hold.release()
            def outer_allocate(size):
                self.meter.grow(self.consumer_arena.token,allocation=size)
                return original_allocate(size)
            def outer_checkpoint():
                return original_checkpoint()
            module.debit_read=outer_read;module.debit_hash=outer_hash;module.reserve_allocation=outer_allocate;module.checkpoint=outer_checkpoint
        self.loaded.append(name)
    def enter(self):
        for name in self.pins:
            if name in sys.modules:
                self.previous[name]=sys.modules.pop(name)
        sys.meta_path.insert(0,self)
        return self
    def module(self,name):
        if name not in self.pins:raise Refused("consumer_module")
        return __import__(name)
    def close(self):
        if self in sys.meta_path:sys.meta_path.remove(self)
        for name in self.pins:sys.modules.pop(name,None)
        sys.modules.update(self.previous)
        self.bodies.clear()
        self.loaded.clear()
        # Imported code/returned JSON may still escape through Root's result.
        # Keep lifetime credit until explicit same-owner delivery retirement.
    def retire_lifetime(self):
        self.pins={};self.previous={};self.bodies={};self.loaded=[]
        if hasattr(self,"residency"):self.residency.release()
        if hasattr(self,"consumer_arena"):self.consumer_arena.release()


class OrdinaryInput:
    """Complete preexisting independently selected five-argument/golden package."""
    FIELDS=("case_id","producer_id","selector_id","observer_id","prepared_ns","unsafe",
        "authority","expected","presented","context","page_files","golden",
        "golden_producer","performing_expected","selection_signature")
    def __init__(self, selected, sources, meter, consumer, actor_id):
        self.selected=exact(selected,self.FIELDS,"ordinary_package")
        self.sources,self.meter,self.consumer=sources,meter,consumer
        self.raw={};self.pages=[];self.records={};self.prepared=False
        self.sources=sources
        if selected["unsafe"] is not False:
            raise Refused("unsafe_historical_id_required_not_run")
        if len({selected["producer_id"],selected["selector_id"],selected["observer_id"],actor_id})!=4:
            raise Refused("ordinary_independence")
        if selected["prepared_ns"]>=mono():raise Refused("ordinary_preexisting")
        for role in ("authority","expected","presented","context","golden"):
            pin=sources[selected[role]]
            if pin["producer_id"]==actor_id:raise Refused("own_derived_oracle")
            maximum=OUTPUT_MAX if role=="golden" else INPUT_MAX
            with Held(pin["path"],pin,meter,maximum,True) as held:self.raw[role]=held.read(maximum)
        for pin in selected["page_files"]:
            exact(pin,("kind","path","page_index","file_path","identity9","sha256"),"ordinary_page")
            if pin["kind"] not in KINDS:raise Refused("ordinary_page_kind")
            self.pages.append(dict(pin))
        if len(self.pages)>512:raise Refused("full_page_capacity")
        self.context=self.consumer("ingress").admit_document(self.raw["context"],"friday.lab824.independent-trust-context.v1")
        self.authority,self.expected,self.presented=self.consumer("ingress").admit_ingress(
            self.raw["authority"],self.raw["expected"],self.raw["presented"])
        if len(self.expected["ubuntu_minimum"]["indexes"])!=13 or len(self.expected["ubuntu_minimum"]["packages"])!=106 or len(self.expected["wheels"]["items"])!=94 or len(self.expected["operations"])!=15:
            raise Refused("full_original_scope")
        if self.context["require_predecessor_receipts"] is not True or not 228<=len(self.context["document_receipts"])<=512 or any(r["body_held"] is not True for r in self.context["document_receipts"]):
            raise Refused("full228_ordinary")
        for name in ("document_contracts","wheel_literal_contracts","performing_contracts"):
            if self.context[name] is None:raise Refused("ordinary_full_prerequisite")
        self.streams=self.consumer("document_windows").FilePageSource(self.pages)
        self.prepared=True

    def golden_check(self):
        producer=exact(self.selected["golden_producer"],(
            "producer_id","selector_id","input_sha256","output_sha256","output_size",
            "resource_observer_id","produced_by_this_package"),"golden_producer")
        if producer["produced_by_this_package"] is not False or producer["producer_id"]!=self.selected["producer_id"] or producer["selector_id"]!=self.selected["selector_id"] or producer["resource_observer_id"]!=self.selected["observer_id"]:
            raise Refused("golden_independence")
        inputs={"authority_sha256":sha(self.raw["authority"],self.meter),"expected_sha256":sha(self.raw["expected"],self.meter),
            "presented_sha256":sha(self.raw["presented"],self.meter),"context_sha256":sha(self.raw["context"],self.meter),
            "selected_pages":self.context["page_sequence"] if self.context["page_sequence"] is not None else self.context["streams"]}
        if producer["input_sha256"]!=domain("friday.a128.full-five-argument-input.v1",inputs,self.meter):
            raise Refused("golden_full_input")
        if sha(self.raw["golden"],self.meter)!=producer["output_sha256"] or len(self.raw["golden"])!=producer["output_size"]:
            raise Refused("golden_full_bytes")
        self.consumer("canonical").parse_exact(self.raw["golden"],max_bytes=OUTPUT_MAX,max_depth=24,max_string=INPUT_MAX)

    def composition(self):
        """Actual unchanged consumers compute the FULL material input preimage."""
        recipe=self.consumer("recipe_planner")
        held=recipe._bind_streams(self.context,self.streams,recipe._ConstructionMeter())
        receipts=recipe._require_receipt_records(self.expected,self.context)
        closure=recipe._consume_closures(self.expected,self.context,held)
        vector=self.consumer("document_vector").assess_document_vector(self.expected,self.context,held,receipts)
        composition=self.consumer("bill").compose_signed_materials(self.expected,
            self.presented["ubuntu_observations"],self.presented["wheel_observations"],receipts)
        composition["wheels"]=self.consumer("material_literals").consume_wheel_literals(
            self.context,held,self.expected["wheels"]["items"],composition["wheels"])
        composition["full_document_vector"]=vector;composition["closure_view"]=closure
        vector["all_materials_closed"]=bool(vector["all_materials_closed"] and
            closure["full_runtime_dependency_custody_joins"]=="STRUCTURALLY_BOUND")
        # Exact A128 recipe_planner wheel normalization, not a reduced view.
        materials={r["path"]:r for r in vector["other_documents"] if r["document_kind"]=="wheel"}
        digest_fn=self.consumer("canonical").domain_digest
        is_digest=self.consumer("contract").is_digest
        for view in composition["wheels"]:
            proof=materials.get(view["filename"])
            closed=proof is not None and proof["installed_observation_status"]=="STRUCTURALLY_BOUND"
            joined=None
            if closed:
                joined=proof["performing_consumer"];body=joined["body"]
                domains={"abi_sha256":digest_fn("friday.a117.abi.v1",body["abi"]),
                    "body_sha256":proof["sha256"],"custody_sha256":joined["custody_sha256"],
                    "installed_inventory_sha256":digest_fn("friday.a117.installed-members.v1",body["members"]),
                    "member_sha256":digest_fn("friday.a117.installed-members.v1",body["members"]),
                    "resource_sha256":digest_fn("friday.a117.resources.v1",body["resources"]),
                    "selected_record_sha256":view["selected_artifact_selector_sha256"]}
                for key,value in domains.items():
                    if view.get(key) is not None and view[key]!=value:raise Refused("wheel_observation")
                closed=all(is_digest(view.get(key)) and view[key]==value for key,value in domains.items())
                view["performing_domain_preimages"]={"abi":body["abi"],"members":body["members"],
                    "resources":body["resources"],"runtime":body["runtime"],"custody":body["custody"]}
            runtime_closed=bool(closed and proof["material_status"]=="STRUCTURALLY_BOUND" and
                joined["runtime_consumer"]["status"]=="STRUCTURALLY_BOUND")
            view["installed_body_runtime_custody_status"]="STRUCTURALLY_BOUND" if runtime_closed else "NOT_PROVEN"
            view["complete_installed_observation_comparison"]="STRUCTURALLY_BOUND" if closed else "NOT_PROVEN"
            view["installed_runtime_remaining_cause"]=None if runtime_closed else "full_future_class_runtime_inputs_absent"
        composition["_held_provider"]=held
        return composition

    def full_binding(self):
        self.golden_check()
        inputs={"authority_sha256":sha(self.raw["authority"],self.meter),"expected_sha256":sha(self.raw["expected"],self.meter),
            "presented_sha256":sha(self.raw["presented"],self.meter),"context_sha256":sha(self.raw["context"],self.meter),
            "selected_pages":self.context["page_sequence"] if self.context["page_sequence"] is not None else self.context["streams"]}
        return {"case_id":self.selected["case_id"],"five_arguments":inputs,
            "full_input_sha256":domain("friday.a128.full-five-argument-input.v1",inputs,self.meter),
            "golden_pin":dict(self.sources[self.selected["golden"]]),
            "golden_producer":dict(self.selected["golden_producer"]),
            "producer_id":self.selected["producer_id"],"selector_id":self.selected["selector_id"],
            "observer_id":self.selected["observer_id"]}

    def add_performing(self, receipt):
        body=receipt["body"];target=receipt["target"];section=receipt["section"]
        ref=receipt["record"]["ref"]
        expected=receipt["expected"]
        # The distinct complete expected body is supplied externally, never set
        # equal to newly observed bytes or computed by this implementation.
        expected_ref={"kind":expected["kind"],"path":expected["logical_path"],
            "sha256":expected["pin"]["sha256"],"size":expected["pin"]["bytes"],
            "producer_id":expected["producer_id"],"selector_id":self.selected["selector_id"],
            "document_sha256":None,"offset":None}
        descriptor={"target":target,"kind":ref["kind"],"path":ref["path"],
            "sha256":receipt["raw_sha256"],"size":receipt["record"]["pin"]["bytes"],
            "producer_id":body["producer_id"],"selector_id":self.selected["selector_id"],
            "document_sha256":None,"offset":None,"expected_body":None,"expected_ref":expected_ref}
        rows=self.context["performing_contracts"][section]
        found=[i for i,r in enumerate(rows) if r["target"]==target]
        if len(found)!=1:raise Refused("complete_descriptor_census")
        rows[found[0]]=descriptor
        self.records[(section,target)]=receipt

    def seal_context(self, page_files, page_sequence):
        if len(page_files)>512 or len(page_sequence)>512:raise Refused("full_page_capacity")
        self.context["page_sequence"]=page_sequence
        # streams is the complete original catalog, not a reduced representative.
        catalog={(r["kind"],r["path"]) for r in self.context["streams"]}
        if not catalog.issubset({(r["kind"],r["path"]) for r in page_sequence}):
            raise Refused("complete_stream_catalog")
        self.raw["context"]=canonical(self.context,self.meter)
        self.consumer("ingress").admit_document(self.raw["context"],"friday.lab824.independent-trust-context.v1")
        self.pages=page_files
        self.streams=self.consumer("document_windows").FilePageSource(page_files)
        # The input SHA changes when actual Root receipts are connected. A
        # preexisting independent full golden must bind those exact final bytes.
        self.golden_check()

    def invoke(self):
        self.golden_check()
        recipe=self.consumer("recipe_planner")
        output=recipe.plan_construction_wire(self.raw["authority"],self.raw["expected"],
            self.raw["presented"],self.raw["context"],self.streams)
        if output!=self.raw["golden"]:raise Refused("complete_public_outcome_mismatch","postdelivery")
        return output

    def invoke_control(self,spec):
        """Actual full ordinary safe variant; no mutation/fixture generator.

        Independent Root selection supplies the COMPLETE already-prepared five
        arguments and full golden. Every original exact tuple remains required.
        Unsafe historical IDs return before any operational Source input/effect.
        """
        if spec["unsafe"] is not False or spec["operational_payload"] is not None:
            raise Refused("unsafe_historical_id_required_not_run")
        self.golden_check()
        try:
            output=self.consumer("recipe_planner").plan_construction_wire(
                self.raw["authority"],self.raw["expected"],self.raw["presented"],
                self.raw["context"],self.streams)
        except self.consumer("contract").ContractError as exc:
            full=getattr(exc,"public_refusal",None)
            if full is None:raise Refused("full_public_refusal_required","postdelivery") from exc
            output=self.consumer("canonical").canonical_bytes(full)
            cause,status,stage=exc.cause,"REFUSED",exc.stage or spec["stage"]
        else:
            full=self.consumer("canonical").parse_exact(output,max_bytes=OUTPUT_MAX,max_depth=24,max_string=INPUT_MAX)
            row=full["stages"][spec["stage"]]
            cause,status,stage=row["cause"],row["status"],spec["stage"]
        matched=(cause!=spec["cause"] if spec["match"]=="unrelated" else cause==spec["cause"])
        if stage!=spec["stage"] or status!=spec["status"] or not matched:
            raise Refused("exact_original_control_tuple_mismatch","postdelivery")
        if output!=self.raw["golden"]:raise Refused("complete_public_outcome_mismatch","postdelivery")
        return output

    def control_variant(self):
        self.golden_check()
        return {"authority_raw":self.raw["authority"],"expected_raw":self.raw["expected"],
            "presented_raw":self.raw["presented"],"context_raw":self.raw["context"],"streams":self.streams,
            "golden_raw":self.raw["golden"],"golden_ref":None,
            "golden_producer":self.selected["golden_producer"],
            "producer_id":self.selected["producer_id"],"selector_id":self.selected["selector_id"]}


def require_all69_and_two_positives(admission, consumer):
    catalog=admission["coverage"]["all69"]
    if len(catalog)!=69 or len({r["id"] for r in catalog})!=69:raise Refused("all69")
    original=consumer("declared_controls").CATALOG
    exact_fields=("id","scenario","status","cause","stage","match")
    if [{k:r[k] for k in exact_fields} for r in catalog]!=[{k:r[k] for k in exact_fields} for r in original]:
        raise Refused("all69_exact_original_tuples")
    if admission["coverage"]["all27"]!=list(range(202,229)) or admission["coverage"]["all5"]!=["A122-R1","A122-R2","A122-R3","A122-R4","A122-R5"] or admission["coverage"]["all9"]!=["P33-"+format(i,"02d") for i in range(1,10)]:
        raise Refused("original_full_scope")
    ordinary=admission["ordinary"]
    required={r["id"] for r in catalog if r["unsafe"] is False}
    if set(ordinary["controls"])!=required or len(ordinary["positives"])!=2:
        raise Refused("complete_independent_ordinary_required")
    if ordinary["positives"][0]["case_id"]==ordinary["positives"][1]["case_id"]:
        raise Refused("two_distinct_full_positives")
    for row in catalog:
        if row["required"] is not True or row["waiver"] is not False:
            raise Refused("control_waiver")
        if row["unsafe"] is True and row["operational_payload"] is not None:
            raise Refused("unsafe_historical_payload")
