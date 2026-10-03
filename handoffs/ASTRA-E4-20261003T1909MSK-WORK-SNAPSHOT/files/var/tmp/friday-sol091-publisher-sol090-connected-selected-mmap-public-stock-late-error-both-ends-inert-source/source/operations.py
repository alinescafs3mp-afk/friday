"""All fifteen performing operation bodies, with original dependency order.

No operation forwards a METHODS label to an unspecified implementation. Native
verbs execute in native.py in the actual Root parent; parsers/installers below
operate on actual same-held authenticated bodies. A publisher gap remains a
real blocked observation, never an invented publisher capability.
"""
import contextlib
import io
import os
import stat
from common import (Refused, INPUT_MAX, DOCUMENT_MAX, MEMBERS_MAX, ARTIFACTS_MAX,
                    exact, integer, text, canonical, parse, domain, sha, mono)
from custody import Held, identity9, open_absolute
from extraction import Installer, deb_install, tar_install, wheel_install, zip_install
from class_semantics import legacy_openpgp, authenticate_retained_transport, native_closure, elf
from admission import ALL15


def _full_counts(expected, inputs):
    if len(expected["ubuntu_minimum"]["indexes"])!=13 or len(expected["ubuntu_minimum"]["packages"])!=106 or len(expected["wheels"]["items"])!=94:
        raise Refused("complete_original_vectors")
    if [r["name"] for r in expected["operations"]]!=list(ALL15):
        raise Refused("all15_original_operations")
    # 350 is the original product artifact cap, not a license to conflate
    # signed metadata/ordinary/golden files with product artifacts.
    artifacts={ (v["kind"],v["logical_path"]) for v in inputs.values()
                if v["kind"] in ("ubuntu-archive","node-archive","wheel","browser","candidate","golden","unrar") }
    if len(artifacts)>ARTIFACTS_MAX or len(inputs)>512:raise Refused("artifact_limit")
    for pin in inputs.values():
        integer(pin["bytes"],DOCUMENT_MAX)


class PerformingOperations:
    def __init__(self, ctx, expected):
        self.ctx,self.expected=ctx,expected
        _full_counts(expected,ctx.sources)
        self.completed,self.raw_auth,self.members,self.events = {},{},[],[]
        self.materials=set()
        self.acquired={}

    def acquire_material_phase(self):
        # Acquire/validate every original class first. No operation-input hash
        # uses an old installed material receipt. The phase has real Root
        # windows and its effects are separately observed, never called a test.
        for name in ALL15[:12]:
            self.ctx.operation=name
            self.ctx.call({"type":"material-acquire","name":name})
            self.acquired[name]=DISPATCH[name](self)
        # Complete original descriptor census, including raw kernel/native/
        # data/candidate/golden bodies not reduced to operation-owned artifacts.
        for descriptor in self.ctx.ordinary.context["performing_contracts"]["materials"]:
            if descriptor["target"]=="a009-custody":continue
            kind,path=descriptor["target"].split("/",1)
            source_id=self.ctx.selected(kind,path)
            if source_id in self.materials:continue
            pin=self.ctx.sources[source_id]
            operations=[name for name in pin["operations"] if name in self.acquired]
            if not operations:raise Refused("full_material_acquisition_owner")
            self.ctx.operation=operations[-1]
            self.ctx.call({"type":"material-current","name":self.ctx.operation})
            self.material(source_id,self.members)
        self.ctx.select_full_material_phase();self.expected=self.ctx.expected

    def run(self, name, actual_input):
        if name not in ALL15 or name in self.completed:raise Refused("operation_order")
        index=ALL15.index(name)
        if any(n not in self.completed for n in ALL15[:index]):raise Refused("operation_dependency")
        exact(actual_input,("operation","material","predecessors"),"operation_input")
        if actual_input["operation"]!=name:raise Refused("operation_input")
        # Each predecessor ref is reached as complete physical bytes, never a
        # caller-supplied digest-only proof.
        for predecessor in actual_input["predecessors"]:
            row=self.completed.get(predecessor["name"])
            if row is None or row["full_output_sha256"]!=predecessor["output_sha256"]:
                raise Refused("operation_dependency")
            with Held(row["record"]["pin"]["path"],row["record"]["pin"],self.ctx.meter,INPUT_MAX,True) as held:
                parse(held.read(INPUT_MAX),self.ctx.meter)
        started=self.ctx.begin(name,actual_input)
        try:
            if name in self.acquired:
                acquired=self.acquired[name]
                owned=[r for r in acquired["members"] if r["path"] in acquired["output_paths"]]
                result=self.outcome(owned,acquired["authentication"],acquired["native_receipts"])
                if "publisher_gap" in acquired:result["publisher_gap"]=acquired["publisher_gap"]
            else:result=DISPATCH[name](self)
            if len(result["output_paths"])<1 or len(self.members)>MEMBERS_MAX:
                raise Refused("complete_operation_members")
            result["input_body"]=actual_input
            result["target"]=name
            result["started_ns"]=started
            result["finished_ns"]=mono()
            receipt=self.ctx.normalize("operations",name,result)
            self.completed[name]=receipt
            self.events.append(receipt)
            return receipt
        except BaseException as exc:
            self.ctx.failure(name,exc)
            raise

    def plan(self, name):
        rows=self.ctx.admission["members"][name]
        if type(rows) is not list or not 1<=len(rows)<=MEMBERS_MAX:raise Refused("member_plan")
        return rows

    def install(self, operation, source_id, codec, literal=None):
        source=self.ctx.sources[source_id]
        selected=[r for r in self.plan(operation) if r["source_id"]==source_id]
        if not selected:raise Refused("complete_member_plan")
        install=self.ctx.new_installer(operation,selected,self.members)
        try:
            with Held(source["path"],source,self.ctx.meter,DOCUMENT_MAX) as held:
                if codec=="deb":
                    members=deb_install(held,install,source["decompressed_max"])
                elif codec=="wheel":
                    members=wheel_install(held,install,literal)
                elif codec=="zip":
                    members=zip_install(held,install)
                else:
                    members=tar_install(held,install,codec,source["decompressed_max"])
                held.check()
        finally:self.ctx.close_installer(install)
        self.add(members)
        return members

    def add(self, rows):
        existing={r["path"]:i for i,r in enumerate(self.members)}
        if len(set(existing)|{r["path"] for r in rows})>MEMBERS_MAX:raise Refused("member_limit")
        for row in rows:
            self.ctx.physical(row)
            if row["path"] in existing:
                previous=self.members[existing[row["path"]]]
                if row["kind"]!="directory" or previous["kind"]!="directory" or row["physical_path"]!=previous["physical_path"] or row["identity9_decimal_strings"][:5]!=previous["identity9_decimal_strings"][:5]:raise Refused("duplicate_member")
                self.members[existing[row["path"]]]=dict(row)
                continue
            existing[row["path"]]=len(self.members)
            self.members.append(row)

    def copy_plan(self,name):
        rows=[]
        for plan in self.plan(name):
            if plan.get("generated_role")=="creation-tool":
                observed=self.ctx.call({"type":"actual-creation-tool-preimage","abi":self.ctx.admission["image"]["abi"]})
                with Held(observed["pin"]["path"],observed["pin"],self.ctx.meter,INPUT_MAX,True) as held:
                    raw=held.read(INPUT_MAX)
                rows.extend(self.ctx.generated_member(name,raw))
                continue
            source=self.ctx.sources[plan["source_id"]]
            installer=self.ctx.new_installer(name,[plan],self.members+rows)
            try:
                if plan["kind"]=="regular":
                    with Held(source["path"],source,self.ctx.meter,DOCUMENT_MAX) as held:
                        if held.body_sha!=plan["sha256"] or held.size!=plan["size"]:
                            raise Refused("member_plan_body")
                        from extraction import FDView
                        with FDView(held) as view:
                            installer.consume(plan["source_name"],"regular",held.size,view,
                                executable=plan["mode"]=="0555")
                elif plan["kind"]=="directory":
                    installer.consume(plan["source_name"],"directory",0)
                elif plan["kind"]=="symlink":
                    installer.consume(plan["source_name"],"symlink",0,target=plan["link_target"])
                else:raise Refused("member_kind")
                rows.extend(installer.seal())
            finally:self.ctx.close_installer(installer)
        self.add(rows);return rows

    def outcome(self, owned, authentication=(), runtime=()):
        if not owned:raise Refused("member_inventory_empty")
        all_members=self.ctx.base_members+list(self.members)
        if len(all_members)>MEMBERS_MAX:raise Refused("member_limit")
        dependencies,facts=native_closure(self.ctx,all_members)
        try:abi=self.ctx.actual_abi(all_members,facts)
        finally:
            keys=[id(fact) for fact in facts.values()];facts.clear()
            for key in keys:self.ctx.meter.retire_result_id(key)
        resources=self.ctx.actual_resources(all_members)
        packages=self.ctx.actual_packages(all_members)
        return {"members":all_members,"output_paths":[r["path"] for r in owned],
            "dependencies":dependencies,"abi":abi,"resources":resources,
            "package_bindings":packages,"authentication":list(authentication),
            "native_receipts":list(runtime),"status":"COMPLETED"}

    def material(self,source_id,owned,authentication=(),runtime=()):
        if source_id in self.materials:return
        self.ctx.retain_material(source_id,self.outcome(owned,authentication,runtime))
        self.materials.add(source_id)

    def ubuntu_indexes(self):
        owned=[];auth=[]
        for index in self.expected["ubuntu_minimum"]["indexes"]:
            signed_id=self.ctx.selected("ubuntu-inrelease",index["id"])
            packages_id=self.ctx.selected("ubuntu-packages",index["id"]+"/"+index["member_name"])
            key_id=self.ctx.sources[signed_id]["authentication"]["keyring_id"]
            proof=legacy_openpgp(self.ctx,signed_id,key_id,"ubuntu-inrelease")
            with Held(self.ctx.sources[packages_id]["path"],self.ctx.sources[packages_id],self.ctx.meter,DOCUMENT_MAX) as packages:
                selected=self.ctx.consumer("formats").parse_release(proof["parsed"]["cleartext"],{
                    k:index[k] for k in ("architecture","component","packages_sha256","size","suite")})
                if selected["packages_sha256"]!=packages.body_sha or selected["size"]!=packages.size:
                    raise Refused("signed_packages_identity")
                raw=packages.read(DOCUMENT_MAX)
                complete=[]
                want=[p for p in self.expected["ubuntu_minimum"]["packages"] if (p["suite"],p["component"])==(index["suite"],index["component"])]
                for package in want:
                    full=self.ctx.consumer("formats").select_package(raw,{
                        k:package[k] for k in ("name","version","architecture","filename","sha256","size")})
                    complete.append(full)
                if len(complete)!=len(want):raise Refused("complete_package_selection")
                self.raw_auth[index["id"]]={"proof":proof,"selected":selected,"packages":complete}
            auth.append(proof)
        if len(self.raw_auth)!=13:raise Refused("full13")
        owned=self.copy_plan("authenticate-ubuntu-indexes")
        self.ctx.publish_raw_auth(self.raw_auth)
        for index in self.expected["ubuntu_minimum"]["indexes"]:
            proof=self.raw_auth[index["id"]]["proof"]
            self.material(self.ctx.selected("ubuntu-inrelease",index["id"]),owned,[proof],[proof["native"]])
            self.material(self.ctx.selected("ubuntu-packages",index["id"]+"/"+index["member_name"]),owned,[proof],[proof["native"]])
        return self.outcome(owned,auth,[p["native"] for p in auth])

    def ubuntu_archives(self):
        owned=[]
        for package in self.expected["ubuntu_minimum"]["packages"]:
            source_id=self.ctx.selected("ubuntu-archive",package["filename"])
            index=next((x for x in self.expected["ubuntu_minimum"]["indexes"] if (x["suite"],x["component"])==(package["suite"],package["component"])),None)
            if index is None or index["id"] not in self.raw_auth:raise Refused("index_link")
            selected=self.raw_auth[index["id"]]["packages"]
            if not any(all(r[k]==package[k] for k in ("name","version","architecture","filename","sha256","size")) for r in selected):
                raise Refused("signed_package_missing")
            pin=self.ctx.sources[source_id]
            if pin["sha256"]!=package["sha256"] or pin["bytes"]!=package["size"]:
                raise Refused("package_archive_identity")
            installed=self.install("authenticate-ubuntu-archives",source_id,"deb")
            owned.extend(installed);self.material(source_id,installed,[self.raw_auth[index["id"]]["proof"]])
        return self.outcome(owned,tuple(self.raw_auth.values()))

    def node_archive(self):
        required=self.expected["node"]
        source_id=self.ctx.selected("node-archive",required["filename"])
        signed_id=self.ctx.selected("node-shasums256","SHASUMS256.txt")
        key_id=self.ctx.sources[signed_id]["authentication"]["keyring_id"]
        with Held(self.ctx.sources[source_id]["path"],self.ctx.sources[source_id],self.ctx.meter,DOCUMENT_MAX) as archive:
            if archive.size!=31_058_332 or required["filename"]!="node-v22.23.2-linux-x64.tar.xz":
                raise Refused("node_archive_identity")
            proof=legacy_openpgp(self.ctx,signed_id,key_id,"node-shasums256",archive)
            authoritative=required["authoritative_sha256"]
            if authoritative is None:raise Refused("node_authoritative_future_required")
            selected=self.ctx.consumer("formats").select_shasum(proof["parsed"]["cleartext"],required["filename"],authoritative)
            if selected["line_sha256"]!=archive.body_sha:raise Refused("node_archive_identity")
        owned=self.install("authenticate-node-archive",source_id,"xz")
        version=self.ctx.native("node-version",[])
        self.ctx.exact_version(version,"v22.23.2\n")
        self.ctx.publish_node_auth(proof,selected)
        self.material(source_id,owned,[proof],[proof["native"],version])
        self.material(signed_id,owned,[proof],[proof["native"]])
        return self.outcome(owned,[proof],[proof["native"],version])

    def unrar_gap(self):
        required=self.expected["unrar"]
        if required["version"]!="7.20" or required["status"]!="BLOCKED_PUBLISHER_GAP":
            raise Refused("unrar_gap_policy")
        # Actual diagnostic artifact and custody are produced; no unrar code is
        # downloaded, unpacked, installed, executed or falsely authenticated.
        gap={"version":"7.20","status":"BLOCKED_PUBLISHER_GAP",
            "publisher_digest":None,"publisher_signature":None,"runtime":"NOT_RUN",
            "effects_granted":False,"source_issued_grant":False}
        owned=self.ctx.generated_member("hold-unrar-publisher-gap",canonical(gap,self.ctx.meter))
        self.add(owned)
        out=self.outcome(owned)
        out["publisher_gap"]=gap
        return out

    def wheels(self):
        owned=[];authentication=[]
        for literal in self.expected["wheels"]["items"]:
            source_id=self.ctx.selected("wheel",literal["filename"])
            proof=authenticate_retained_transport(self.ctx,source_id,("pypi.org","files.pythonhosted.org"))
            metadata=proof["metadata"]
            # Complete literal metadata is consumed, never a nonempty issuer tag.
            expected=self.ctx.literal_wheel(literal)
            if metadata["filename"]!=literal["filename"] or metadata["name"]!=literal["name"] or metadata["version"]!=literal["version"] or metadata["sha256"]!=literal["sha256"]:
                raise Refused("wheel_publisher_identity")
            contracts=[r for r in self.ctx.ordinary.context["wheel_literal_contracts"] if r["filename"]==literal["filename"]]
            if len(contracts)!=1 or metadata["metadata_raw_sha256"]!=contracts[0]["raw_document_sha256"]:
                raise Refused("full_pypi_metadata_preimage")
            if "requires_python_raw_info" not in metadata or "requires_python_raw_url" not in metadata:
                raise Refused("complete_pypi_literals_required")
            contract=contracts[0]
            if metadata["requires_python_raw_info"]!=contract["package_info_requires_python_raw"] or metadata["requires_python_raw_url"]!=contract["selected_artifact_requires_python_raw"]:
                raise Refused("requires_python_literal_preservation")
            for body_key,ref_key,actual_key in (("package_info_selector_body","package_info_selector_ref","raw_info"),
                ("selected_artifact_selector_body","selected_artifact_selector_ref","raw_selected_url")):
                selected=self.ctx.consumer("performing_contracts").expected_body(self.ctx.raw_held,contract[body_key],contract[ref_key])
                if selected!=metadata[actual_key]:raise Refused("full_pypi_selector")
            authentication.append(proof)
            installed=self.install("authenticate-wheels",source_id,"wheel",expected)
            owned.extend(installed)
            self.ctx.publish_wheel_literal(literal,metadata,proof)
            self.material(source_id,installed,[proof],[proof["actual_signature"]])
        if len(authentication)!=94:raise Refused("full94")
        return self.outcome(owned,authentication)

    def venv(self):
        wanted=b"home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.14.4\n"
        source_id=self.ctx.selected("native","venv/pyvenv.cfg")
        with Held(self.ctx.sources[source_id]["path"],self.ctx.sources[source_id],self.ctx.meter,INPUT_MAX) as held:
            if held.read(INPUT_MAX)!=wanted:raise Refused("venv_config")
        owned=self.copy_plan("map-cpython-venv")
        runtime=self.ctx.native("python-version",[])
        self.ctx.exact_version(runtime,"Python 3.14.4\n")
        return self.outcome(owned,runtime=[runtime])

    def dynload(self):
        owned=self.copy_plan("map-lib-dynload")
        for row in owned:
            if row["kind"]=="regular":
                with Held(row["physical_path"],self.ctx.pin(row),self.ctx.meter,DOCUMENT_MAX) as held:
                    metadata=elf(held)
                    if metadata["architecture"]!="amd64":raise Refused("python_abi")
        return self.outcome(owned)

    def loader(self):
        owned=self.copy_plan("map-native-loader")
        runtime=self.ctx.native("loader-list",[])
        if runtime["exit_code"]!=0:raise Refused("loader_runtime","execution")
        event=self.outcome(owned,runtime=[runtime])
        self.ctx.retain_closure("native",event)
        for source_id in self.ctx.class_sources("native"):self.material(source_id,owned,runtime=[runtime])
        return event

    def browser(self):
        expected=self.expected["browser"]
        if expected["playwright"]!="1.61.0" or expected["chromium"]!="1228" or expected["headless"]!="1228" or expected["ffmpeg"]!="1011":
            raise Refused("browser_revision")
        owned=[];auth=[]
        for source_id in self.ctx.class_sources("browser"):
            proof=authenticate_retained_transport(self.ctx,source_id,("cdn.playwright.dev","playwright.download.prss.microsoft.com"))
            if proof["metadata"]["revision"] not in ("1228","1011"):raise Refused("browser_revision")
            auth.append(proof)
            owned.extend(self.install("map-browser-resources",source_id,self.ctx.sources[source_id]["codec"]))
        runtime=self.ctx.native("browser-version",[])
        if runtime["exit_code"]!=0:raise Refused("browser_runtime","execution")
        self.ctx.verify_browser_resources(owned,expected)
        for source_id in self.ctx.class_sources("browser"):self.material(source_id,owned,auth,[runtime])
        return self.outcome(owned,auth,[runtime])

    def data(self):
        owned=self.copy_plan("map-data-closure")
        self.ctx.verify_data_packages(owned,self.expected["data_packages"])
        resources=self.ctx.actual_resources(self.ctx.base_members+self.members)
        if {r["role"] for r in resources}!={"certificates","fonts","locales","timezones","nss","glib","gpu"}:
            raise Refused("data_roles")
        event=self.outcome(owned)
        self.ctx.retain_closure("data",event)
        for source_id in self.ctx.class_sources("data"):self.material(source_id,owned)
        return event

    def candidate(self):
        required=self.expected["candidate"]
        if required["commit"]!="cecd28a92ac4fd4e34c4d3813d0c598debe09436" or required["tree"]!="e7e86c7796a3db6571ffea715e666247f8876d63":
            raise Refused("candidate_identity")
        self.ctx.verify_tree("candidate",required)
        owned=self.copy_plan("bind-candidate")
        for source_id in self.ctx.class_sources("candidate"):self.material(source_id,owned)
        return self.outcome(owned)

    def golden(self):
        required=self.expected["golden"]
        if required["commit"]!="5497d28dedf49b219fcc2112d6b391ba6cbbc690":
            raise Refused("golden_identity")
        self.ctx.verify_tree("golden",required)
        owned=self.copy_plan("bind-golden")
        for source_id in self.ctx.class_sources("golden"):self.material(source_id,owned)
        return self.outcome(owned)

    def assemble(self):
        # Full final member bytes from every prior operation, not predecessor
        # digest labels, are re-held before assembly.
        self.ctx.verify_all_members(self.members)
        projection=self.ctx.consumer_member_projection(self.completed)
        owned=self.ctx.generated_member("assemble-members",canonical(projection,self.ctx.meter))
        self.add(owned);return self.outcome(owned)

    def manifest(self):
        self.ctx.verify_all_members(self.members)
        projection=self.ctx.consumer_member_projection(self.completed)
        owned=self.ctx.generated_member("write-final-manifest",canonical(projection,self.ctx.meter))
        self.add(owned)
        self.ctx.verify_a009_final_domains(self.members)
        return self.outcome(owned)

    def custody(self):
        self.ctx.verify_all_members(self.members)
        observation=self.ctx.independent_outer()
        if observation["implicit_io_status"]!="OBSERVED":raise Refused("actual_outer_unknown")
        body={"root_owned_observation":observation,
              "full_predecessor_refs":[r["record"]["pin"] for r in self.completed.values()],
              "image_inventory":self.ctx.admission["image"]["members"],
              "effects_granted":False,"source_issued_grant":False}
        owned=self.ctx.generated_member("external-custody",canonical(body,self.ctx.meter))
        self.add(owned);return self.outcome(owned)


DISPATCH={
    "authenticate-ubuntu-indexes":PerformingOperations.ubuntu_indexes,
    "authenticate-ubuntu-archives":PerformingOperations.ubuntu_archives,
    "authenticate-node-archive":PerformingOperations.node_archive,
    "hold-unrar-publisher-gap":PerformingOperations.unrar_gap,
    "authenticate-wheels":PerformingOperations.wheels,
    "map-cpython-venv":PerformingOperations.venv,
    "map-lib-dynload":PerformingOperations.dynload,
    "map-native-loader":PerformingOperations.loader,
    "map-browser-resources":PerformingOperations.browser,
    "map-data-closure":PerformingOperations.data,
    "bind-candidate":PerformingOperations.candidate,
    "bind-golden":PerformingOperations.golden,
    "assemble-members":PerformingOperations.assemble,
    "write-final-manifest":PerformingOperations.manifest,
    "external-custody":PerformingOperations.custody,
}
