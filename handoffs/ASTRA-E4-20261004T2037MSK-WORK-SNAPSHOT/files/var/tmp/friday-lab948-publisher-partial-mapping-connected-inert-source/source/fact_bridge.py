"""Full fresh graph joins. Selection is independent; observations are not oracles.

Material acquisition precedes ANY called operation-input derivation. A single
independently signed phase package selects all complete material/closure/raw
contracts. All original consumers still consume their full original bodies.
No old installed observation is borrowed for a new actor.
"""
from common import Refused, INPUT_MAX, OUTPUT_MAX, DOCUMENT_MAX, canonical, parse, sha, domain
from custody import Held
from consumer_bridge import OrdinaryInput


def physical_value(value,meter,depth=0):
    if depth>24:raise Refused("physical_graph_depth")
    if type(value) is dict:
        if set(value)=={"physical_byte_preimage"}:
            pin=value["physical_byte_preimage"]["pin"]
            with Held(pin["path"],pin,meter,DOCUMENT_MAX,True) as held:
                return held.read(DOCUMENT_MAX)
        return {k:physical_value(v,meter,depth+1) for k,v in value.items()}
    if type(value) is list:return [physical_value(v,meter,depth+1) for v in value]
    return value


def read_record(pin,meter,maximum=INPUT_MAX):
    with Held(pin["path"],pin,meter,maximum,True) as held:
        return parse(held.read(maximum),meter,maximum)


def fresh_graph(parent):
    return {"schema":"friday.sol053.complete-retained-fact-graph.v1",
        "actor_id":parent.actor_id,"case_id":parent.case["case_id"],
        "admission_nonce":parent.admission["nonce"],
        "source_manifest_sha256":parent.admission["source_manifest_sha256"],
        "consumer_manifest_sha256":parent.admission["consumer_manifest_sha256"],
        "typed":list(parent.retained_receipts),
        "authentication":parent.authentication,"legacy_closures":parent.legacy_closures,
        "physical_inputs":parent.admission["inputs"],"generated_plans":parent.generated_plans,
        "page_files":parent.page_files,"page_sequence":parent.page_sequence,
        "operation_windows":parent.operation_windows,"effects_granted":False}


def join_typed(parent,ordinary,sections):
    recipe=parent.consumer("recipe_planner")
    held=recipe._bind_streams(ordinary.context,ordinary.streams,recipe._ConstructionMeter())
    produced={(r["section"],r["target"]):r for r in parent.retained_receipts if r["section"] in sections}
    descriptors={(section,r["target"]):r for section in sections
        for r in ordinary.context["performing_contracts"][section]}
    # A009 custody is intentionally late, not a material-phase body.
    if sections==("materials","closures"):
        descriptors.pop(("materials","a009-custody"),None)
    if set(produced)!=set(descriptors):raise Refused("fresh_full_descriptor_census")
    joins={}
    for key,receipt in produced.items():
        descriptor=descriptors[key];pin=receipt["record"]["pin"]
        if descriptor["sha256"]!=pin["sha256"] or descriptor["size"]!=pin["bytes"]:
            raise Refused("fresh_descriptor_full_pin")
        observed=receipt["record"]["ref"]
        wanted={"kind":observed["kind"],"path":observed["path"],"sha256":pin["sha256"],
            "size":pin["bytes"],"document_sha256":observed.get("document_sha256"),"offset":observed.get("offset")}
        if {k:descriptor[k] for k in wanted}!=wanted:raise Refused("fresh_complete_descriptor_slice_ref")
        page_key=(wanted["kind"],wanted["path"],0)
        root_page=[p for p in parent.page_files if (p["kind"],p["path"],p["page_index"])==page_key]
        selected_page=[p for p in ordinary.pages if (p["kind"],p["path"],p["page_index"])==page_key]
        if len(root_page)!=1 or selected_page!=root_page:raise Refused("fresh_observed_physical_page_custody")
        body=read_record(pin,parent.observer)
        if descriptor["producer_id"]!=parent.actor_id:raise Refused("fresh_actor_producer")
        actual_input=body["input_body"]
        if key[0]=="materials" and key[1]!="a009-custody":
            match=[p for p in ordinary.sources.values() if p["kind"]+"/"+p["logical_path"]==key[1]]
            if len(match)!=1:raise Refused("fresh_raw_material_census")
            raw=match[0]
            if actual_input!={"document_kind":raw["kind"],"path":raw["logical_path"],"sha256":raw["sha256"],"size":raw["bytes"]}:
                raise Refused("fresh_material_input_join")
        joined=parent.consumer("performing_contracts").consume(ordinary.context,held,*key,actual_input)
        if joined is None or joined["body"]!=body or joined["raw_sha256"]!=pin["sha256"]:
            raise Refused("fresh_complete_typed_body_join")
        if joined["runtime_consumer"]["full_body_consumed"] is not True:
            raise Refused("fresh_complete_runtime_join")
        joins[key]=joined
    for role,pin in parent.legacy_closures.items():
        raw=read_record(pin,parent.observer)
        wire=canonical(raw,parent.observer)
        chosen=ordinary.context["closure_bodies"][role]
        contract=ordinary.expected[role+"_closure"]
        if chosen.encode("ascii")!=wire or contract["legacy_body_sha256"]!=sha(wire,parent.observer):
            raise Refused("fresh_legacy_closure_full_body")
        if contract["body_sha256"]!=produced[("closures",role)]["record"]["pin"]["sha256"]:
            raise Refused("fresh_typed_closure_contract")
    return joins


def join_authentication(parent,ordinary):
    contracts={(r["document_kind"],r["path"]):r for r in ordinary.context["document_contracts"]}
    seen_ubuntu=set();seen_wheels=set();seen_node=False;ubuntu_proofs=[]
    recipe=parent.consumer("recipe_planner")
    held=recipe._bind_streams(ordinary.context,ordinary.streams,recipe._ConstructionMeter())
    native={r["pid"]:r["terminal"] for r in parent.native.invocations if "terminal" in r}
    def actual_native(record):
        if record.get("pid") not in native or native[record["pid"]]!=record or record["exit_code"]!=0:
            raise Refused("fresh_authentication_actual_Root_native_receipt")
    raw_rows={(r["document_kind"],r["path"]):r for r in ordinary.context["document_receipts"]}
    for key,row in raw_rows.items():
        if key not in contracts:continue
        selected=[p for p in parent.admission["inputs"].values() if (p["kind"],p["logical_path"])==key]
        if len(selected)!=1 or row["sha256"]!=selected[0]["sha256"] or row["size"]!=selected[0]["bytes"]:
            raise Refused("fresh_full_raw_receipt_physical_inputs")
    for operation,pins in parent.authentication.items():
        for pin in pins:
            value=physical_value(read_record(pin,parent.observer),parent.observer)
            if operation=="authenticate-ubuntu-indexes":
                for name,row in value.items():
                    if name in seen_ubuntu:raise Refused("fresh_duplicate_authentication")
                    seen_ubuntu.add(name);proof=row["proof"];contract=contracts[("ubuntu-inrelease",name)]
                    actual_native(proof["native"]);ubuntu_proofs.append((name,row,contract))
                    for field,source in (("capability","capability"),("verification_result","verification_result")):
                        if contract[field]!=proof[source]:raise Refused("fresh_raw_authentication_full_result")
                    if contract["custody_body"]!=canonical(proof["custody_body"],parent.observer).decode("ascii"):
                        raise Refused("fresh_raw_authentication_custody")
                    selected=parent.consumer("performing_contracts").expected_body(
                        held,contract["selected_body"],contract["selected_ref"])
                    if selected!=row["selected"]:raise Refused("fresh_raw_selected_release")
            elif operation=="authenticate-node-archive":
                if seen_node:raise Refused("fresh_duplicate_authentication")
                seen_node=True;proof=value["proof"];contract=contracts[("node-shasums256","SHASUMS256.txt")]
                actual_native(proof["native"])
                if contract["capability"]!=proof["capability"] or contract["verification_result"]!=proof["verification_result"]:
                    raise Refused("fresh_node_full_raw_authentication")
                for field,actual in (("node_capability",proof["capability"]),("node_result",proof["verification_result"])):
                    if ordinary.presented[field]!=actual:raise Refused("fresh_presented_node_full_authentication")
                source=next(p for p in parent.admission["inputs"].values() if p["kind"]=="node-shasums256")
                with Held(source["path"],source,parent.observer,INPUT_MAX) as raw:
                    if recipe._bytes(ordinary.presented["node_clearsign"])!=raw.read(INPUT_MAX):raise Refused("fresh_presented_node_full_body")
                required=ordinary.expected["node"]
                selection=parent.consumer("formats").select_shasum(proof["parsed"]["cleartext"],required["filename"],required["authoritative_sha256"])
                if selection!=value["selection"]:raise Refused("fresh_full_node_selected_row")
                authoritative=ordinary.presented["node_authoritative"]
                if authoritative is None or {"filename":selection["filename"],"sha256":selection["line_sha256"],"size":31058332} not in authoritative["rows"]:
                    raise Refused("fresh_presented_node_authoritative_row")
            elif operation=="authenticate-wheels":
                literal=value["literal"];metadata=value["metadata"];name=literal["filename"]
                actual_native(value["proof"]["actual_signature"])
                if name in seen_wheels:raise Refused("fresh_duplicate_authentication")
                seen_wheels.add(name)
                selected=[r for r in ordinary.context["wheel_literal_contracts"] if r["filename"]==name]
                if len(selected)!=1:raise Refused("fresh_full_wheel_literal_census")
                c=selected[0]
                if c["raw_document_sha256"]!=metadata["metadata_raw_sha256"] or c["package_info_requires_python_raw"]!=metadata["requires_python_raw_info"] or c["selected_artifact_requires_python_raw"]!=metadata["requires_python_raw_url"]:
                    raise Refused("fresh_full_wheel_literals")
                for body_key,ref_key,actual_key in (("package_info_selector_body","package_info_selector_ref","raw_info"),("selected_artifact_selector_body","selected_artifact_selector_ref","raw_selected_url")):
                    if parent.consumer("performing_contracts").expected_body(held,c[body_key],c[ref_key])!=metadata[actual_key]:raise Refused("fresh_complete_wheel_selected_bodies")
    if len(seen_ubuntu)!=13 or len(seen_wheels)!=94 or not seen_node:
        raise Refused("fresh_full_raw_authentication_census")
    presented=[row for name,row,contract in ubuntu_proofs if contract["release_expected"]==ordinary.presented["release_expected"]]
    if len(presented)!=1:raise Refused("fresh_presented_ubuntu_selection")
    proof=presented[0]["proof"]
    if ordinary.presented["ubuntu_capability"]!=proof["capability"] or ordinary.presented["ubuntu_result"]!=proof["verification_result"]:
        raise Refused("fresh_presented_ubuntu_full_authentication")
    selected_name=next(name for name,row,contract in ubuntu_proofs if row is presented[0])
    source=next(p for p in parent.admission["inputs"].values() if p["kind"]=="ubuntu-inrelease" and p["logical_path"]==selected_name)
    with Held(source["path"],source,parent.observer,INPUT_MAX) as raw:
        if recipe._bytes(ordinary.presented["ubuntu_clearsign"])!=raw.read(INPUT_MAX):raise Refused("fresh_presented_ubuntu_full_body")
    if recipe._bytes(ordinary.presented["release_cleartext"])!=proof["parsed"]["cleartext"]:raise Refused("fresh_presented_release_cleartext")
    # Re-run every original metadata/receipt/literal consumer on selected fresh
    # inputs. Expected capabilities/result pins remain independent selections.
    composition=ordinary.composition()
    if composition["full_document_vector"]["all_materials_closed"] is not True or composition["closure_view"]["full_runtime_dependency_custody_joins"]!="STRUCTURALLY_BOUND":
        raise Refused("fresh_complete_material_composition")
    return composition


def selected_phase(parent,pin):
    from independent_selector import selected_after_retention
    graph=fresh_graph(parent)
    if read_record(pin,parent.observer,OUTPUT_MAX)!=graph:raise Refused("actor_root_complete_fact_graph")
    parent.retained_receipts.append({"section":"material-phase","target":parent.case["case_id"],"record":{"pin":pin},"retained_ns":__import__("common").mono()})
    selected=selected_after_retention(parent,"material-phase",parent.case["case_id"],pin,"material-phase")
    ordinary=OrdinaryInput(selected["ordinary"],selected["sources"],parent.observer,parent.consumer,parent.actor_id)
    if ordinary.selected["case_id"]!=parent.case["case_id"] or ordinary.selected["selector_id"]!=parent.selector_id:
        raise Refused("phase_original_case")
    join_typed(parent,ordinary,("materials","closures"));join_authentication(parent,ordinary)
    ordinary.golden_check()
    parent.phase_ordinary=ordinary
    return selected


def validate_final(parent,ordinary,public_pin):
    from admission import ALL15
    join_typed(parent,ordinary,("materials","closures","operations","snapshot"))
    join_authentication(parent,ordinary)
    produced=read_record(public_pin,parent.observer,OUTPUT_MAX)
    if [r["name"] for r in produced]!=list(ALL15):raise Refused("fresh_final_all15_order")
    catalog={r["id"]:r for r in parent.admission["coverage"]["all69"]}
    output=ordinary.invoke_control(catalog[ordinary.selected["case_id"]]) if ordinary.selected["case_id"] in catalog else ordinary.invoke()
    value=parent.consumer("canonical").parse_exact(output,max_bytes=OUTPUT_MAX,max_depth=24,max_string=INPUT_MAX)
    if value["full_operation_outputs"]!=produced:raise Refused("fresh_complete_public_predecessor_join")
    projection=parent.consumer("recipe_planner").final_manifest_projection(produced)
    if value["manifest_projection"]!=projection or value["manifest_sha256"]!=projection["manifest_sha256"]:
        raise Refused("fresh_full_generated_manifest_join")
    for name,plans in parent.generated_plans.items():
        row=next(p for p in produced if p["name"]==name)
        for plan in plans:
            physical=[m for m in row["members"] if m["path"]==plan["logical_path"]]
            if len(physical)!=1 or physical[0]["content_sha256"]!=plan["sha256"] or physical[0]["size"]!=plan["size"]:
                raise Refused("fresh_generated_exact_plan_join")
    parent.final_binding=ordinary.full_binding()
    parent.final_expected_output=sha(output,parent.observer)
    parent.final_public_pin=public_pin
    return output
