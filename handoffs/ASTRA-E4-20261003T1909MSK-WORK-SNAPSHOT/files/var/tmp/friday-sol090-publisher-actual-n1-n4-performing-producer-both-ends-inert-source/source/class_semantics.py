"""Class-specific authentication, complete native semantics and physical preimages."""
import base64
import hashlib
import re
import struct
import json
from common import Refused, INPUT_MAX, DOCUMENT_MAX, exact, parse, canonical, domain, sha, digest, integer
from custody import Held

UBUNTU_FINGERPRINT="F6ECB3762474EDA9D21B7022871920D1991BC93C"
UBUNTU_KEYRING_SHA="80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
UBUNTU_GPGV_SHA="95ecd00d02b79d091f103af175b1dbf95b5b0c66503104bfcea447cca7e3829d"
NODESOURCE_NOT_AUTHORITY="6F71F525282841EEDAF851B42F59B5F99B1BE0B4"


def signature_algorithm(armor):
    """A bounded actual signature packet, not a header/issuer algorithm label."""
    lines=armor.splitlines()
    if len(armor)>INPUT_MAX or not lines or lines[0]!=b"-----BEGIN PGP SIGNATURE-----":
        raise Refused("signature_armor")
    at=1
    while at<len(lines) and lines[at]:
        if b":" not in lines[at]:raise Refused("signature_armor")
        at+=1
    if at>=len(lines):raise Refused("signature_armor")
    at+=1;parts=[]
    while at<len(lines) and lines[at] and not lines[at].startswith(b"="):
        if len(lines[at])>76:raise Refused("signature_armor")
        parts.append(lines[at]);at+=1
    if at+1>=len(lines) or not lines[at].startswith(b"=") or lines[at+1]!=b"-----END PGP SIGNATURE-----" or at+2!=len(lines):
        raise Refused("signature_armor")
    packet=base64.b64decode(b"".join(parts),validate=True)
    crc=0xB704CE
    for c in packet:
        crc^=c<<16
        for _ in range(8):
            crc<<=1
            if crc&0x1000000:crc^=0x1864CFB
    if base64.b64decode(lines[at][1:],validate=True)!=(crc&0xffffff).to_bytes(3,"big"):
        raise Refused("signature_crc")
    if not packet or packet[0]&0x80==0:raise Refused("signature_packet")
    if packet[0]&0x40:
        tag=packet[0]&0x3f;p=1
        if p>=len(packet):raise Refused("signature_packet")
        n=packet[p];p+=1
        if n<192:size=n
        elif n<224:
            if p>=len(packet):raise Refused("signature_packet")
            size=(n-192)*256+packet[p]+192;p+=1
        elif n==255:
            if p+4>len(packet):raise Refused("signature_packet")
            size=int.from_bytes(packet[p:p+4],"big");p+=4
        else:raise Refused("signature_packet_length")
    else:
        tag=(packet[0]>>2)&15;mode=packet[0]&3;p=1
        if mode==3:raise Refused("signature_packet_length")
        count=(1,2,4)[mode]
        if p+count>len(packet):raise Refused("signature_packet")
        size=int.from_bytes(packet[p:p+count],"big");p+=count
    body=packet[p:]
    if tag!=2 or size!=len(body) or len(body)<6 or body[0]!=4 or body[1]!=1:
        raise Refused("signature_packet")
    algorithm={8:"openpgp-sha256",10:"openpgp-sha512"}.get(body[3])
    if algorithm is None:raise Refused("unsupported_hash")
    return algorithm


def gpgv_fingerprint(stderr):
    # The fixed four-argument A128 invocation is retained exactly. Default gpgv
    # diagnostics in LC_ALL=C carry the full signer fingerprint. Truncated or
    # ambiguous output is refused rather than adding undocumented argv flags.
    raw=stderr.decode("utf-8")
    fingerprints=re.findall(r"using [A-Za-z0-9/-]+ key ([0-9A-F]{40})(?:\s|$)",raw)
    if len(fingerprints)!=1 or "Good signature" not in raw or "BAD signature" in raw:
        raise Refused("full_signature_fingerprint")
    return fingerprints[0]


def legacy_openpgp(ctx, source_id, keyring_id, kind, archive=None):
    source=ctx.sources[source_id];keyring=ctx.sources[keyring_id]
    with Held(source["path"],source,ctx.meter,INPUT_MAX) as raw, \
         Held(keyring["path"],keyring,ctx.meter,INPUT_MAX) as keys:
        body=raw.read(INPUT_MAX)
        parsed=ctx.consumer("formats").parse_clearsign(body)
        packet_algorithm=signature_algorithm(parsed["signature_armor"])
        if packet_algorithm!="openpgp-"+parsed["hash_header"].lower():
            raise Refused("algorithm_correspondence")
        verb="ubuntu-gpgv" if kind=="ubuntu-inrelease" else "node-gpgv"
        native=ctx.native(verb,[source_id,keyring_id])
        if native["exit_code"]!=0:raise Refused("signature_failed","execution")
        with Held(native["stderr"]["pin"]["path"],native["stderr"]["pin"],ctx.meter,INPUT_MAX,True) as errors:
            fingerprint=gpgv_fingerprint(errors.read(INPUT_MAX))
        approved=source["authentication"]["approved_fingerprint"]
        if fingerprint!=approved or fingerprint==NODESOURCE_NOT_AUTHORITY:
            raise Refused("publisher_fingerprint")
        if kind=="ubuntu-inrelease" and (fingerprint!=UBUNTU_FINGERPRINT or keys.body_sha!=UBUNTU_KEYRING_SHA or native["tool_pin"]["sha256"]!=UBUNTU_GPGV_SHA):
            raise Refused("ubuntu_authority_pin")
        issuer="ubuntu-archive" if kind=="ubuntu-inrelease" else "nodejs.org"
        custody={"document_kind":kind,"path":source["logical_path"],"sha256":raw.body_sha,
                 "size":raw.size,"produced_by_this_package":False}
        custody_sha=domain("friday.sol037.document-custody.v1",custody,ctx.meter)
        dep_rows=[]
        tool=ctx.admission["tools"][verb]
        for pin in tool["dependencies"]:
            with Held(pin["path"],pin,ctx.meter,DOCUMENT_MAX) as dep:
                s=dep.before
                dc={"producer_id":ctx.actor_id,"path":pin["logical_path"],"sha256":dep.body_sha,
                    "size":dep.size,"effects_granted":False}
                dep_rows.append({"path":pin["logical_path"],"sha256":dep.body_sha,
                    "size":dep.size,"mode":format(int(s[2])&0o777,"04o"),"uid":int(s[3]),"gid":int(s[4]),
                    "custody_sha256":domain("friday.a138.physical-tool-custody.v1",dc,ctx.meter),"status":"BOUND"})
        if not dep_rows:raise Refused("dependency_inventory_empty")
        closure=domain("friday.lab815.dependency-closure.v1",
            [{k:r[k] for k in ("custody_sha256","gid","mode","path","sha256","size","uid")}
             for r in sorted(dep_rows,key=lambda r:r["path"])],ctx.meter)
        env=[{"name":k,"value":v} for k,v in sorted(native["environment"].items())]
        environment=domain("friday.lab815.environment.v1",env,ctx.meter)
        attestation={"issuer_id":issuer,"actual_root_actor":ctx.actor_id,
            "raw_document":raw.pin_now(),"keyring":keys.pin_now(),"signature_algorithm":packet_algorithm,
            "approved_fingerprint":approved,"actual_native_receipt":native,"dependency_preimages":dep_rows,
            "custody_body":custody,"effects_granted":False}
        attestation_record=ctx.put_body("signature-attestation",attestation)
        attested={"issuer_id":issuer,"attestation_sha256":domain("friday.a138.publisher-signature-attestation.v1",attestation,ctx.meter),
            "custody_sha256":custody_sha,"signature_sha256":sha(parsed["signature_armor"],ctx.meter),
            "produced_by_this_package":False}
        cap={"schema":"friday.lab815.verifier-capability.v1","algorithm_class":packet_algorithm,
            "argv":native["argv"],"dependencies":dep_rows,"dependency_closure_sha256":closure,
            "dependency_status":"BOUND","document_kind":kind,"environment_digest":environment,
            "environment_entries":env,"executable":native["argv"][0],
            "executable_sha256":native["tool_pin"]["sha256"],"issuer_attestation":attested,
            "key_fingerprint":fingerprint,"keyring_sha256":keys.body_sha,
            "produced_by_this_package":False,"signer_supplied_by_candidate":False,"verified_by_tool":True}
        archive_sha=archive.body_sha if archive is not None else None
        archive_size=archive.size if archive is not None else None
        result={"schema":"friday.lab815.verification-result.v1","algorithm":packet_algorithm,
            "archive_sha256":archive_sha,"archive_size":archive_size,"argv":list(native["argv"]),
            "armor_sha256":sha(parsed["signature_armor"],ctx.meter),"attestation_sha256":attested["attestation_sha256"],
            "body_sha256":sha(parsed["normalized_body"],ctx.meter),"capability_sha256":ctx.consumer("capability").capability_digest(cap),
            "custody_sha256":custody_sha,"decision":"AUTHENTICATED","dependency_closure_sha256":closure,
            "environment_digest":environment,"issuer_id":issuer,"issuer_result_sha256":attested["signature_sha256"],
            "keyring_sha256":keys.body_sha,"produced_by_this_package":False,"raw_sha256":raw.body_sha}
        raw.check();keys.check()
        return {"parsed":parsed,"capability":cap,"verification_result":result,
            "custody_body":custody,"attestation_record":attestation_record,"native":native}


def publisher_json(raw,meter):
    """Parse complete bounded real publisher JSON, preserve raw bytes exactly.

    Publisher bytes need not be Source canonical JSON. Capacity/duplicate/non-
    finite checks precede any allocation; only Root's own records use parse().
    """
    from common import json_preflight
    lexical=raw if raw.endswith(b"\n") else raw+b"\n"
    allocation=json_preflight(lexical,INPUT_MAX,24)
    hold=meter.reserve("publisher-json-before-allocation",reads=len(raw),allocation=allocation)
    def pairs(items):
        result={}
        if len(items)>512:raise Refused("publisher_json_items")
        for k,v in items:
            if k in result:raise Refused("publisher_json_duplicate")
            result[k]=v
        return result
    def reject(_):raise Refused("publisher_json_nonfinite")
    try:
        value=json.loads(raw.decode("utf-8"),object_pairs_hook=pairs,parse_constant=reject)
        hold.commit(reads=len(raw));meter.own_result(value,hold);hold=None
        return value
    finally:
        if hold is not None:hold.release()


def authenticate_retained_transport(ctx, source_id, allowed_hosts):
    """Full independently signed retained TLS/HTTP evidence plus same held body."""
    source=ctx.sources[source_id]
    auth=source["authentication"]
    exact(auth,("kind","receipt_id","signature_id","key_id"),"transport_auth")
    if auth["kind"] not in ("independent-root-retained-https","independent-root-retained-https-bundle"):raise Refused("transport_auth")
    receipt_source=ctx.sources[auth["receipt_id"]]
    if receipt_source["producer_id"]==ctx.actor_id:raise Refused("self_retained_transport")
    with Held(receipt_source["path"],receipt_source,ctx.meter,INPUT_MAX,True) as receipt, \
         Held(source["path"],source,ctx.meter,DOCUMENT_MAX) as body:
        envelope=parse(receipt.read(INPUT_MAX),ctx.meter)
        if auth["kind"]=="independent-root-retained-https-bundle":
            exact(envelope,("schema","producer_id","selected_by","records","effects_granted"),"retained_bundle")
            if envelope["schema"]!="friday.root.retained-https-bundle.v1" or envelope["effects_granted"] is not False or not 1<=len(envelope["records"])<=512:
                raise Refused("retained_bundle")
            chosen=[r for r in envelope["records"] if r["source_id"]==source_id]
            if len(chosen)!=1 or len({r["source_id"] for r in envelope["records"]})!=len(envelope["records"]):raise Refused("retained_bundle_complete")
            record=dict(chosen[0]);record.pop("source_id")
        else:record=envelope
        record=exact(record,(
            "schema","producer_id","selected_by","host","url","method","status",
            "peer_certificate_sha256","trust_anchor_sha256","tls_version","hostname_verified",
            "raw_request_ref","raw_response_headers_ref","body_sha256","body_size",
            "metadata_ref","observed_ns","effects_granted"),"retained_transport")
        if record["schema"]!="friday.root.retained-https.v1" or record["host"] not in allowed_hosts or record["method"]!="GET" or record["status"]!=200 or record["hostname_verified"] is not True or record["tls_version"] not in ("TLSv1.2","TLSv1.3"):
            raise Refused("publisher_transport")
        if record["producer_id"]!=receipt_source["producer_id"] or record["selected_by"]!=ctx.selector_id or record["producer_id"]==ctx.actor_id or record["effects_granted"] is not False:
            raise Refused("publisher_transport_owner")
        if record["body_sha256"]!=body.body_sha or record["body_size"]!=body.size:
            raise Refused("publisher_transport_body")
        for key in ("peer_certificate_sha256","trust_anchor_sha256"):digest(record[key])
        if record["trust_anchor_sha256"] not in ctx.admission["image"]["vendor_trust_anchors"]:
            raise Refused("publisher_transport_anchor")
        cache=(auth["receipt_id"],auth["signature_id"],auth["key_id"],receipt.body_sha)
        verified=ctx.transport_verified.get(cache)
        if verified is None:
            verified=ctx.native("retained-signature",[auth["receipt_id"],auth["signature_id"],auth["key_id"]])
            ctx.transport_verified[cache]=verified
        if verified["exit_code"]!=0:raise Refused("retained_signature","execution")
        # The complete HTTP request/header/metadata bodies are reached and held.
        for ref_name in ("raw_request_ref","raw_response_headers_ref","metadata_ref"):
            ref=record[ref_name]
            exact(ref,("source_id","sha256","size"),"retained_preimage")
            pin=ctx.sources[ref["source_id"]]
            with Held(pin["path"],pin,ctx.meter,DOCUMENT_MAX) as preimage:
                if preimage.body_sha!=ref["sha256"] or preimage.size!=ref["size"]:
                    raise Refused("retained_preimage")
                if ref_name=="metadata_ref":
                    metadata_raw=preimage.read(INPUT_MAX)
                    metadata=publisher_json(metadata_raw,ctx.meter)
                    metadata_pin=preimage.pin_now()
        if "info" in metadata and "urls" in metadata:
            info=metadata["info"]
            selected=[r for r in metadata["urls"] if r["filename"]==source["logical_path"]]
            if len(selected)!=1:raise Refused("complete_selected_pypi_url")
            url=selected[0]
            # Literal null/empty values remain distinct in full raw_info/url.
            metadata={"name":re.sub(r"[-_.]+","-",info["name"]).lower(),"version":info["version"],
                "filename":url["filename"],"url":url["url"],"sha256":url["digests"]["sha256"],
                "size":url["size"],"requires_python_raw_info":info["requires_python"],
                "requires_python_raw_url":url["requires_python"],"raw_info":info,"raw_selected_url":url,
                "metadata_raw_sha256":sha(metadata_raw,ctx.meter),"full_metadata_pin":metadata_pin}
        else:
            metadata=dict(metadata);metadata["metadata_raw_sha256"]=sha(metadata_raw,ctx.meter);metadata["full_metadata_pin"]=metadata_pin
        if record["url"]!=metadata["url"] or metadata["sha256"]!=body.body_sha or metadata["size"]!=body.size:
            raise Refused("publisher_metadata")
        return {"record":record,"metadata":metadata,"actual_signature":verified,
                "body_pin":body.pin_now(),"receipt_pin":receipt.pin_now()}


def elf(held):
    raw=held.read(DOCUMENT_MAX)
    hold=held.meter.reserve("ELF-returned-metadata-lifetime-before-parse",allocation=4_194_304)
    try:
        value=_elf_metadata(held,raw)
        held.meter.own_result(value,hold);hold=None
        return value
    finally:
        key=id(raw);raw=None;held.meter.retire_result_id(key)
        if hold is not None:hold.release()


def _elf_metadata(held,raw):
    """Read actual ELF64 AMD64 program/dynamic tables without running member code."""
    if len(raw)<64 or raw[:6]!=b"\x7fELF\x02\x01":raise Refused("native_abi")
    fields=struct.unpack_from("<HHIQQQIHHHHHH",raw,16)
    kind,machine,version,entry,phoff,shoff,flags,ehsize,phsize,phnum,shsize,shnum,shstr=fields
    if kind not in (2,3) or machine!=62 or version!=1 or ehsize!=64 or phsize!=56 or not 1<=phnum<=128 or phoff+phnum*56>len(raw):
        raise Refused("native_abi")
    loads=[];dynamic=None;interpreter=None
    for i in range(phnum):
        tag,pflags,offset,vaddr,paddr,filesz,memsz,align=struct.unpack_from("<IIQQQQQQ",raw,phoff+i*56)
        if filesz>memsz or offset+filesz>len(raw):raise Refused("elf_segment")
        if tag==1:loads.append((vaddr,filesz,offset))
        elif tag==2:
            if dynamic is not None or filesz%16:raise Refused("elf_dynamic")
            dynamic=(offset,filesz)
        elif tag==3:
            if interpreter is not None or not 1<=filesz<=240:raise Refused("elf_interpreter")
            value=raw[offset:offset+filesz]
            if not value.endswith(b"\0"):raise Refused("elf_interpreter")
            interpreter=value[:-1].decode("ascii")
    needed=[];soname=None;rpaths=[];strtab=None;strsz=None
    tags=[]
    if dynamic is not None:
        at,size=dynamic
        if size//16>512:raise Refused("elf_dynamic")
        terminated=False
        for p in range(at,at+size,16):
            tag,value=struct.unpack_from("<qQ",raw,p)
            if tag==0:terminated=True;break
            tags.append((tag,value))
            if tag==5:strtab=value
            if tag==10:strsz=value
        if not terminated:raise Refused("elf_dynamic")
    if tags:
        if strtab is None or strsz is None or strsz>2_000_000:raise Refused("elf_strings")
        ranges=[off+strtab-v for v,sz,off in loads if v<=strtab and strtab+strsz<=v+sz]
        if len(ranges)!=1:raise Refused("elf_strings")
        base=ranges[0]
        def string(offset):
            if offset>=strsz:raise Refused("elf_string")
            end=raw.find(b"\0",base+offset,base+strsz)
            if end<0 or end-base-offset>240:raise Refused("elf_string")
            return raw[base+offset:end].decode("ascii")
        for tag,value in tags:
            if tag==1:needed.append(string(value))
            elif tag==14:soname=string(value)
            elif tag in (15,29):rpaths.append(string(value))
    if len(set(needed))!=len(needed):raise Refused("dependency_inventory")
    held.check()
    return {"architecture":"amd64","interpreter":interpreter,"needed":needed,
            "soname":soname,"rpaths":rpaths,"sha256":held.body_sha,"size":held.size}


def native_closure(ctx, members):
    by_path={r["path"]:r for r in members}
    libraries={}
    facts={}
    for row in members:
        if row["kind"]!="regular" or row["path"] not in ctx.admission["image"]["elf_members"]:continue
        with Held(row["physical_path"],{"path":row["physical_path"],"sha256":row["sha256"],"bytes":row["size"],
                  "identity9_decimal_strings":row["identity9_decimal_strings"]},ctx.meter,DOCUMENT_MAX) as held:
            fact=elf(held);facts[row["path"]]=fact
            if fact["soname"]:
                if fact["soname"] in libraries:raise Refused("duplicate_native_provider")
                libraries[fact["soname"]]=row
    edges=[]
    for path,fact in facts.items():
        for soname in fact["needed"]:
            provider=libraries.get(soname)
            if provider is None:raise Refused("dependency_closure")
            edges.append({"consumer_path":path,"provider_path":provider["path"],
                "provider_sha256":provider["sha256"],"abi":"cp314-regular"})
        for rpath in fact["rpaths"]:
            if rpath not in ctx.admission["image"]["allowed_rpaths"]:
                raise Refused("native_rpath")
        if fact["interpreter"] is not None and fact["interpreter"] not in by_path:
            raise Refused("native_loader")
    if len(edges)>512:raise Refused("dependency_inventory")
    return edges,facts
