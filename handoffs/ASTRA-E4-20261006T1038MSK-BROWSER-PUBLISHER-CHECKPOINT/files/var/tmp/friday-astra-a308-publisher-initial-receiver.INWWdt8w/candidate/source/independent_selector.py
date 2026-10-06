"""Finite actual Root retention -> external signed selection -> continuation.

Source never copies an observed body into expected, runs an oracle, produces a
selector signature, or grants itself authority. The existing independent Root
selector owner publishes exact expected physical preimages and a detached
signature inside its separately enrolled private inbox. Source holds the actor
and all costs while waiting on kernel notification, with one finite deadline.
"""
import ctypes
import os
import selectors
import stat
from common import Refused,INPUT_MAX,OUTPUT_MAX,exact,parse,mono
from custody import Held,open_absolute,identity9
from lifetime import OwnedFDs


def selected_after_retention(parent,section,target,observed_pin,sequence):
    selected=parent.admission["independent_selector"]
    root=selected["directory"]
    hold=parent.observer.reserve("independent-selector-input-lifetime",reads=INPUT_MAX*3,
        allocation=INPUT_MAX*256+65536,slots=4)
    book=OwnedFDs(credit=hold,meter=parent.observer)
    parent.observer.retain_local_owner(book)
    directory=watch=-1
    try:
        directory=open_absolute(root,os.O_RDONLY|os.O_DIRECTORY,journal=book)
        s=os.fstat(directory)
        if identity9(s)[:6]!=selected["directory_identity9"][:6] or s.st_uid!=0 or stat.S_IMODE(s.st_mode)!=0o700:
            raise Refused("independent_selector_custody")
        library=ctypes.CDLL(None,use_errno=True)
        def acquire_watch():
            opened=library.inotify_init1(os.O_CLOEXEC|os.O_NONBLOCK)
            if opened<0:raise OSError(ctypes.get_errno(),"selector-inotify")
            return opened
        watch=book.acquire(acquire_watch,holder="selector-inotify",credit=hold)
        wd=library.inotify_add_watch(watch,("/proc/self/fd/"+str(directory)).encode(),0x00000008|0x00000080|0x00000400|0x00000800)
        if wd<0:raise OSError(ctypes.get_errno(),"selector-watch")
        name="selection-"+str(sequence)+".json"
        # Check AFTER watch installation to avoid an arrival race. This is a
        # bounded host-kernel wait, never a model/mailbox polling workflow.
        with selectors.PollSelector() as ready:
            ready.register(watch,selectors.EVENT_READ)
            while True:
                try:
                    candidate=os.stat(name,dir_fd=directory,follow_symlinks=False)
                except FileNotFoundError:candidate=None
                if candidate is not None:break
                parent.observer.check()
                left=(parent.observer.reserve_deadline-mono())/1e9
                if left<=0:raise Refused("independent_selector_deadline","postdelivery")
                if not ready.select(min(left,0.25)):continue
                raw=os.read(watch,65536);hold.commit(reads=len(raw))
                if not raw:raise Refused("independent_selector_watch_lost","postdelivery")
        if not stat.S_ISREG(candidate.st_mode) or candidate.st_uid!=0 or candidate.st_nlink!=1 or stat.S_IMODE(candidate.st_mode)!=0o600:
            raise Refused("independent_selector_file")
        pin={"path":root+"/"+name,"bytes":candidate.st_size,
            "identity9_decimal_strings":identity9(candidate)}
        with Held(pin["path"],None,parent.observer,INPUT_MAX,True) as record:
            raw=record.read(INPUT_MAX);pin=record.pin_now()
            fields=(
                "schema","issuer_id","selector_id","expected_producer_id","actor_id","admission_nonce",
                "source_manifest_sha256","consumer_manifest_sha256","section","target","observed_pin",
                "expected_pin","logical_path","selected_ns","effects_granted","produced_by_this_package")
            if section in ("final-public","material-phase"):fields+=("final_ordinary","final_source_pins")
            if section=="generated-member":fields+=("generated_member_plan",)
            value=exact(parse(raw,parent.observer),fields,"independent_selected_record")
            if value["schema"]!="friday.a138.independently-selected-performing-body.v1" or value["effects_granted"] is not False or value["produced_by_this_package"] is not False:
                raise Refused("independent_selected_record")
            if value["issuer_id"]!=selected["issuer_id"] or value["selector_id"]!=parent.selector_id or value["expected_producer_id"]!=parent.case["producer_id"] or value["actor_id"]!=parent.actor_id:
                raise Refused("independent_selector_identity")
            if value["expected_producer_id"] in (parent.actor_id,parent.observer.observer_id) or value["admission_nonce"]!=parent.admission["nonce"]:
                raise Refused("independent_selector_admission")
            if value["source_manifest_sha256"]!=parent.admission["source_manifest_sha256"] or value["consumer_manifest_sha256"]!=parent.admission["consumer_manifest_sha256"]:
                raise Refused("independent_selector_snapshot")
            if value["section"]!=section or value["target"]!=target or value["observed_pin"]!=observed_pin:
                raise Refused("independent_selector_exact_observation")
            if not parent.retained_receipts[-1]["retained_ns"]<=value["selected_ns"]<=mono():raise Refused("independent_selector_order")
            signature_path=pin["path"]+".sig"
            with Held(signature_path,None,parent.observer,INPUT_MAX,True) as signature, \
                 Held(selected["key_pin"]["path"],selected["key_pin"],parent.observer,INPUT_MAX,True) as key:
                verification=parent.native.verify_selected_input(record,signature,key)
            if verification["exit_code"]!=0:raise Refused("independent_selector_signature")
        expected=value["expected_pin"]
        if not expected["path"].startswith(root+"/") or expected["path"]==observed_pin["path"]:
            raise Refused("independent_expected_custody")
        maximum=OUTPUT_MAX if section in ("final-public","material-phase") else INPUT_MAX
        with Held(expected["path"],expected,parent.observer,maximum,True) as body:
            if body.body_sha!=observed_pin["sha256"] or body.size!=observed_pin["bytes"]:raise Refused("independent_full_expected_bytes")
        parent.selector_receipts.append({"selected_record_pin":pin,"actual_signature":verification,"selected_ns":value["selected_ns"]})
        out={"pin":expected,"kind":"member","logical_path":value["logical_path"],"producer_id":value["expected_producer_id"]}
        if section in ("final-public","material-phase"):
            sources=dict(parent.admission["inputs"])
            for source_id,pin in value["final_source_pins"].items():
                if source_id in sources and sources[source_id]!=pin:raise Refused("final_input_pin_substitution")
                if not pin["path"].startswith(root+"/"):raise Refused("final_independent_input_custody")
                sources[source_id]=pin
            if len(sources)>512:raise Refused("final_full_input_capacity")
            out.update({"ordinary":value["final_ordinary"],"sources":sources})
        if section=="generated-member":out["generated_member_plan"]=value["generated_member_plan"]
        return out
    finally:
        book.close()
        pending=[{"fd":fd,"holder":book.meta.get(fd,{}).get("holder"),"credit":book.meta.get(fd,{}).get("credit"),
            "identity9_decimal_strings":book.meta.get(fd,{}).get("identity9_decimal_strings"),
            "status":book.meta.get(fd,{}).get("status")} for fd in sorted(book.fds)]
        unknown=any(row.get("status")=="UNKNOWN" for row in book.meta.values())
        if pending or unknown:
            parent.observer.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fds":pending})
        else:
            hold.release()
            parent.observer.retire_local_owner(book)
