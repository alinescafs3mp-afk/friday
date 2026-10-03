"""Inert inherited obligations; SOURCE_INCOMPLETE. Never loaded by preparation.

The original whole source is preserved byte-for-byte as UPSTREAM-A066-SNAPSHOT-UPSTREAM-A061-CONTROLS.py.
It contains historical fixture constructors and Python seams which this package
does not turn into native whole-control proof. No operational negative payload,
root service, launcher fallback or authority writer is provided here.
"""

PINS = {}
HELD_BYTES = {}
x = s = None

import ctypes
import hashlib
import io
import json
import os
import errno
import resource
import select
import signal
import struct
import time
import a061_contract as c

NATIVE_INPUT_SCHEMA="friday.a079.ordinary-owned-native-input.v1"
NATIVE_CASES=("positive","wait_flags_DATA","caller_status_DATA")
ORDINARY_BYTES=tuple(("ordinary-owned-a079/%d\n"%i).encode("ascii") for i in range(3))

def input_DATA(raw):
    data=x.m.inert_json(raw,4096)
    c.need(type(data) is dict and set(data)=={"schema","case","workers"} and
        data["schema"]==NATIVE_INPUT_SCHEMA and data["case"] in NATIVE_CASES and
        data["workers"]==[{"payload":v.decode("ascii"),"exit":17+i} for i,v in enumerate(ORDINARY_BYTES)],
        "A079_INDEPENDENT_ORDINARY_INPUT")
    return data

def observation(pid=None):
    out=c.NativeObservation()
    result=c.NATIVE.fr_session_observe(ctypes.byref(out)) if pid is None else c.NATIVE.fr_fixture_observe(pid,ctypes.byref(out))
    c.need(result==0,"A079_ACTUAL_NATIVE_OBSERVATION")
    return {name:getattr(out,name) for name,_ in out._fields_}

def controls_native(raw):
    data=input_DATA(raw);cap=c.ADMISSION
    c.need(cap is not None and cap["mode_id"]==1 and c.NATIVE is not None,"A079_NATIVE_PUBLIC_START")
    before=observation();parent,birth=c.proc(os.getpid());_,origin_birth=c.proc(parent)
    c.need(before["owner"]==os.getpid() and before["origin"]==parent and
        before["owner_birth"]==birth and before["origin_birth"]==origin_birth and
        before["uid"]==os.getuid()==1000 and before["gid"]==os.getgid()==1000 and
        before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2,
        "A079_FIXED_ACTUAL_SESSION_ORIGIN")
    children=[];pipes=[];count=3 if data["case"]=="positive" else 1
    end=min(cap["work_ns"],time.monotonic_ns()+15*10**9)
    try:
        for role in range(count):
            ready_read,ready_write=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)
            gate_read,gate_write=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)
            pipes.extend((ready_read,ready_write,gate_read,gate_write))
            pid=c.NATIVE.fr_fixture_fork(123)
            c.need(pid>=0,"A079_ACTUAL_CLONE_AND_REGISTER_ACK")
            if pid==0:
                try:
                    for fd in pipes:
                        if fd not in (ready_write,gate_read):os.close(fd)
                    # This return is AFTER the unchanged native REGISTER_ACK
                    # release barrier, not a READY label granting authority.
                    c.need(os.write(ready_write,ORDINARY_BYTES[role])==len(ORDINARY_BYTES[role]),"A079_ORDINARY_WRITE")
                    os.close(ready_write)
                    while time.monotonic_ns()<end:
                        if select.select([gate_read],[],[],.005)[0]:
                            c.need(os.read(gate_read,1)==b"G","A079_ORDINARY_GATE")
                            os.close(gate_read);os._exit(17+role)
                    os._exit(124)
                except BaseException:os._exit(125)
            children.append({"pid":pid,"read":ready_read,"gate":gate_write,"payload":b""})
            for fd in (ready_write,gate_read):os.close(fd);pipes.remove(fd)
            initial=observation(pid);actual_parent,actual_birth=c.proc(pid)
            with open("/proc/%d/status"%pid,encoding="ascii") as stream:credentials=stream.read(65537)
            c.need(len(credentials)<=65536 and
                "Uid:\t1000\t1000\t1000\t1000\n" in credentials and
                "Gid:\t1000\t1000\t1000\t1000\n" in credentials,"A079_ACTUAL_CHILD_UID_GID")
            c.need(initial["pid"]==pid and initial["role"]==role and initial["state"]==2 and
                initial["birth"]==actual_birth and actual_parent==os.getpid() and initial["status_known"]==0 and
                initial["wait_observed"]==0 and initial["cleanup_reaped"]==0 and initial["next_sequence"]==4+2*role,
                "A079_NATIVE_CREATED_REGISTERED_UNKNOWN_STATUS")
            children[-1]["registered"]=initial
        for role,child in enumerate(children):
            while time.monotonic_ns()<end:
                if select.select([child["read"]],[],[],.005)[0]:
                    part=os.read(child["read"],65)
                    if not part:break
                    child["payload"]+=part;c.need(len(child["payload"])<=64,"A079_ORDINARY_BYTE_BOUND")
            else:raise c.Refused("A079_READY_DEADLINE")
            c.need(child["payload"]==ORDINARY_BYTES[role],"A079_INDEPENDENT_EXACT_ORDINARY_BYTES")
            os.close(child["read"]);pipes.remove(child["read"])
        rows=[]
        if data["case"]=="positive":
            for child in children:c.need(os.write(child["gate"],b"G")==1,"A079_GATE_RELEASE")
            for role,child in enumerate(children):
                status=ctypes.c_int(-1)
                result=c.NATIVE.fr_fixture_wait(123,child["pid"],0,ctypes.byref(status))
                after=observation(child["pid"])
                c.need(result==child["pid"] and status.value==(17+role)<<8 and
                    after["state"]==3 and after["status_known"]==1 and after["wait_observed"]==1 and
                    after["cleanup_reaped"]==1 and after["handle_closed"]==1 and after["pidfd"]==-1 and
                    after["status"]==(17+role)<<8 and after["creation_poisoned"]==0 and
                    after["next_sequence"]==9+role,"A079_EXACT_ACTUAL_WAIT4_ACK_CLOSE")
                rows.append({"role":role,"payload":ORDINARY_BYTES[role].decode("ascii"),"exit":17+role,
                    "registered":child["registered"],"final":after})
        else:
            child=children[0];status=ctypes.c_int(-1)
            refusal=c.NATIVE.fr_fixture_wait(123,child["pid"],0x40000000,ctypes.byref(status)) if data["case"]=="wait_flags_DATA" else c.NATIVE.fr_fixture_reap(123,child["pid"],17<<8)
            c.need(refusal==(-22 if data["case"]=="wait_flags_DATA" else -1),"A079_SINGLE_INERT_CALL_DATA_REFUSAL")
            poisoned=observation(child["pid"])
            c.need(poisoned["state"]==4 and poisoned["creation_poisoned"]==1 and
                poisoned["status_known"]==0 and poisoned["wait_observed"]==0 and
                c.NATIVE.fr_fixture_fork(123)==-117,"A079_SAME_GENERATION_NO_NEW_CLONE")
            stop=c.NATIVE.fr_fixture_stop(123,child["pid"],min(cap["hard_ns"]-10**9,time.monotonic_ns()+10**9))
            after=observation(child["pid"])
            c.need(stop==-117 and after["state"]==4 and after["status_known"]==0 and after["status"]==0 and
                after["wait_observed"]==1 and after["cleanup_reaped"]==1 and after["handle_closed"]==1 and
                after["pidfd"]==-1 and after["creation_poisoned"]==1,"A079_EXACT_OWN_UNKNOWN_DISPOSAL_NO_ACK_CREDIT")
            rows.append({"role":0,"payload":ORDINARY_BYTES[0].decode("ascii"),"registered":child["registered"],
                "refusal_errno":refusal,"poisoned":poisoned,"stop_errno":stop,"final":after,"wait_status":None})
        return {"schema":"friday.a079.native-public-subset-result.v1","native_registry_only":True,
            "case":data["case"],"ordinary_rows":rows,"session_before":before,"session_after":observation(),
            "terminal_completion":data["case"]=="positive","body_complete":False,"acceptance_complete":False,
            "current_GO":False,"all216":"NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION",
            "full_authoritative_registry_controls":"SOURCE_INCOMPLETE","F10_waiver":False}
    finally:
        uncertain=False
        for child in children:
            state=observation(child["pid"])
            if not state["cleanup_reaped"] or not state["handle_closed"]:
                c.NATIVE.fr_fixture_stop(123,child["pid"],min(cap["hard_ns"]-10**9,time.monotonic_ns()+10**9))
                state=observation(child["pid"])
            if not state["cleanup_reaped"] or not state["handle_closed"]:uncertain=True
        for fd in pipes:
            try:os.close(fd)
            except OSError:uncertain=True
        if uncertain:raise c.Refused("A079_STOP_UNCONFIRMED")

def controls(plan, raw):
    cap=c.ADMISSION
    if cap is not None and cap["mode_id"]==1:
        fixture=c.held(119,cap["pins"]["fixture"],1048576)
        decoded=x.m.inert_json(fixture,1048576)
        if type(decoded) is dict and decoded.get("schema")==NATIVE_INPUT_SCHEMA:return controls_native(fixture)
        if type(decoded) is dict and decoded.get("schema")==PUBLIC_INPUT_SCHEMA:return controls_public(fixture)
        if type(decoded) is dict and decoded.get("schema")=="friday.a091.receiving-public-input.v1":return route_receiving_fixture(fixture, decoded)
        if type(decoded) is dict and decoded.get("schema")==WHOLE216_INPUT_SCHEMA:return controls_whole216(fixture,plan)
    # The protected native mode1 caller reaches a precise, persistent refusal.
    # Invented native ownership/cause labels cannot fulfill the 216 obligations.
    raise s.Refused("SOURCE_INCOMPLETE_INHERITED_NATIVE_CONTROLS_NOT_ADMITTED")

PUBLIC_INPUT_SCHEMA="friday.a087.native-public-input.v1"
PUBLIC_CASES=("positive","start_origin_pid","start_origin_uid","start_origin_gid","start_owner","start_owner_birth",
    "start_deadline","start_role","start_sequence","start_rights","ack_origin_pid","ack_origin_uid","ack_origin_gid",
    "ack_owner","ack_owner_birth","ack_session","ack_role","ack_sequence_replay","ack_sequence_future","ack_deadline",
    "ack_rights","ack_many_rights","register_invalid_ACK","register_lost_ACK","register_delayed_ACK","abort_positive",
    "abort_invalid_ACK","abort_lost_ACK","reap_invalid_ACK","reap_lost_ACK","copy_pid","copy_pidfd","copy_birth","copy_role",
    "pidfd_plaintext","pidfd_closed","signal_denied","close_denied","transport_closed","role_invalid","deadline_expired",
    "session_restart","wait_flags_DATA","caller_status_DATA")
PUBLIC_BYTES=tuple(("ordinary-owned-a087/%d\n"%i).encode("ascii") for i in range(3))
READY=struct.Struct("<QiiiiQQ")

def public_observe(child):
    out=c.NativeObservation();c.need(c.NATIVE.fr_own_observe(ctypes.byref(child),ctypes.byref(out))==0,"A087_NATIVE_BOUND_OBSERVATION")
    return {key:getattr(out,key) for key,_ in out._fields_}

def public_evidence(child=None):
    out=c.NativeEvidence()
    result=c.NATIVE.fr_session_evidence(ctypes.byref(out)) if child is None else c.NATIVE.fr_own_evidence(ctypes.byref(child),ctypes.byref(out))
    c.need(result==0,"A087_PRIVATE_NATIVE_EVIDENCE");return c.evidence_DATA(out)

def public_wait(child,end):
    while time.monotonic_ns()<end:
        result=c.NATIVE.fr_own_wait(123,ctypes.byref(child),1)
        if result:return result
        select.select([],[],[],.002)
    raise c.Refused("A087_OWN_WAIT_DEADLINE")

def public_resources():
    return {"self_peak_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        "children_peak_bytes":resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024,
        "AS":list(resource.getrlimit(resource.RLIMIT_AS)),"CPU":list(resource.getrlimit(resource.RLIMIT_CPU)),
        "NOFILE":list(resource.getrlimit(resource.RLIMIT_NOFILE)),"FSIZE":list(resource.getrlimit(resource.RLIMIT_FSIZE)),
        "CORE":list(resource.getrlimit(resource.RLIMIT_CORE)),"affinity":sorted(os.sched_getaffinity(0))}

def controls_public(raw):
    data=x.m.inert_json(raw,4096)
    c.need(type(data) is dict and set(data)=={"schema","case","workers"} and data["schema"]==PUBLIC_INPUT_SCHEMA and
        data["case"] in PUBLIC_CASES and data["workers"]==[{"payload":v.decode("ascii"),"exit":23+i} for i,v in enumerate(PUBLIC_BYTES)],
        "A087_ORDINARY_PUBLIC_INPUT")
    case=data["case"];cap=c.ADMISSION;lib=c.NATIVE
    c.need(cap is not None and cap["mode_id"]==1 and lib is not None and not case.startswith("start_"),"A087_REAL_NATIVE_START")
    before=observation();c.need(before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2,
        "A087_PUBLIC_PREREQUISITE")
    end=min(cap["work_ns"],time.monotonic_ns()+20*10**9)
    children=[];pipes=set();rows=[];attempts=[];primary=None;transport=None;closed_plaintext=False
    completed=case in ("positive","register_delayed_ACK","abort_positive")
    try:
        if case.startswith("abort_"):
            # Exhaust only this coordinator's own descriptor allowance AFTER
            # startup. One slot is left for actual proc/socket verification;
            # native pipe2 needs two and fails in the real kernel with EMFILE.
            old=resource.getrlimit(resource.RLIMIT_NOFILE);held=[];target=256
            c.need(old==(512,512),"A087_ORIGINAL_DESCRIPTOR_ENVELOPE")
            resource.setrlimit(resource.RLIMIT_NOFILE,(target,old[1]))
            try:
                while True:
                    try:held.append(os.dup(100))
                    except OSError as exc:
                        c.need(exc.errno==24,"A087_ACTUAL_EMFILE");break
                c.need(bool(held),"A087_EXHAUSTION_PREREQUISITE");os.close(held.pop())
                failed=c.Child();result=lib.fr_own_fork(123,0,end,ctypes.byref(failed))
                c.need(failed.pid==0 and failed.pidfd==-1 and failed.creation_errno==24,"A087_REAL_NO_CHILD_CREATION_FAILURE")
                primary={"operation":"creation","result":result,"record":public_observe(failed),"evidence":public_evidence(failed)}
                attempts.append(primary)
            finally:
                for fd in held:os.close(fd)
                resource.setrlimit(resource.RLIMIT_NOFILE,old)
            if not completed:
                c.need(result<0,"A087_ABORT_REPLY_REFUSAL")
                other=c.Child();again=lib.fr_own_fork(123,0,end,ctypes.byref(other))
                attempts.append({"operation":"after_refusal_creation","result":again,"created_pid":other.pid})
                c.need(again<0 and other.pid==0,"A087_ABORT_UNKNOWN_NO_SECOND_CLONE")
                return public_terminal(case,before,rows,attempts,primary,transport,False)
            c.need(result==-24 and failed.state==0,"A087_ABORT_ACK_CONFIRMED_NO_CLONE")
        count=3 if completed else 1
        for role in range(count):
            ready_read,ready_write=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)
            gate_read,gate_write=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK);pipes.update((ready_read,ready_write,gate_read,gate_write))
            intention=c.Child();pid=lib.fr_own_fork(123,role,end,ctypes.byref(intention))
            if pid==0:
                try:
                    for fd in pipes:
                        if fd not in (ready_write,gate_read):os.close(fd)
                    parent,birth=c.proc(os.getpid());_,parent_birth=c.proc(parent)
                    message=READY.pack(time.monotonic_ns(),os.getpid(),parent,os.getuid(),os.getgid(),birth,parent_birth)+PUBLIC_BYTES[role]
                    c.need(os.write(ready_write,message)==len(message),"A087_REAL_POST_ACK_READY");os.close(ready_write)
                    while time.monotonic_ns()<end:
                        if select.select([gate_read],[],[],.002)[0]:
                            value=os.read(gate_read,1)
                            if value==b"G":os.close(gate_read);os._exit(23+role)
                            os._exit(124)
                    os._exit(124)
                except BaseException:os._exit(125)
            for fd in (ready_write,gate_read):os.close(fd);pipes.remove(fd)
            row={"role":role,"spawn_result":pid,"intention":intention,"read":ready_read,"gate":gate_write,
                "registered":public_observe(intention),"spawn_evidence":public_evidence(intention),"ready":None,"payload":""}
            children.append(row)
            if pid<0:
                primary={"operation":"fork","result":pid,"record":row["registered"],"evidence":row["spawn_evidence"]}
                c.need(intention.pid>=0,"A087_DURABLE_ACTUAL_CLONE_INTENTION");break
        for child in children:
            content=bytearray()
            while time.monotonic_ns()<end:
                if select.select([child["read"]],[],[],.002)[0]:
                    part=os.read(child["read"],129)
                    if not part:break
                    content.extend(part);c.need(len(content)<=128,"A087_ORDINARY_READY_BOUND")
            else:raise c.Refused("A087_READY_DRAIN_DEADLINE")
            os.close(child["read"]);pipes.remove(child["read"])
            if child["spawn_result"]>0:
                c.need(len(content)==READY.size+len(PUBLIC_BYTES[child["role"]]) and content[READY.size:]==PUBLIC_BYTES[child["role"]],"A087_INDEPENDENT_ORDINARY_PAYLOAD")
                child["ready"]=list(READY.unpack(content[:READY.size]));child["payload"]=content[READY.size:].decode("ascii")
            else:c.need(not content,"A087_NO_READY_BEFORE_REGISTER_ACK")
        if completed:
            for child in children:c.need(os.write(child["gate"],b"G")==1,"A087_OWN_GATE_RELEASE")
            for child in children:
                result=public_wait(child["intention"],end);released=lib.fr_own_release(ctypes.byref(child["intention"]))
                child.update(wait_result=result,release_result=released)
        elif primary is None:
            child=children[0];record=child["intention"]
            operation="wait";result=None
            if case.startswith("copy_"):
                field=case[5:];setattr(record,field,getattr(record,field)+1)
                result=lib.fr_own_wait(123,ctypes.byref(record),1)
            elif case in ("pidfd_plaintext","pidfd_closed"):
                native_fd=record.pidfd
                if case=="pidfd_plaintext":
                    plain=os.memfd_create("ordinary-own-plaintext-a087",os.MFD_CLOEXEC)
                    os.write(plain,b"ordinary-owned-plaintext-a087\n");os.dup2(plain,native_fd);os.close(plain)
                    # Actual descriptor number now names an owned plain file;
                    # native cleanup must not close it as if it were a pidfd.
                    pipes.add(native_fd)
                else:os.close(native_fd)
                operation="stop";result=lib.fr_own_stop(123,ctypes.byref(record),min(end,time.monotonic_ns()+10**9))
                c.need(os.write(child["gate"],b"G")==1,"A087_STALE_HANDLE_CHILD_ORDINARY_EXIT")
            elif case=="signal_denied":
                c.need(lib.fr_local_restrict(1,-1)==0,"A087_REAL_LOCAL_SIGNAL_RESTRICTION")
                operation="stop";result=lib.fr_own_stop(123,ctypes.byref(record),min(end,time.monotonic_ns()+500000000))
                c.need(os.write(child["gate"],b"G")==1,"A087_RESTRICTED_SIGNAL_OWN_EXIT")
            elif case in ("reap_invalid_ACK","reap_lost_ACK","close_denied"):
                c.need(os.write(child["gate"],b"G")==1,"A087_OWN_EXIT_BEFORE_PRIVATE_WAIT")
                result=public_wait(record,end)
                if case=="close_denied":
                    c.need(result==1 and lib.fr_local_restrict(2,record.pidfd)==0,"A087_REAL_LOCAL_CLOSE_RESTRICTION")
                    operation="release";result=lib.fr_own_release(ctypes.byref(record))
            elif case=="transport_closed":
                operation="transport";result=lib.fr_session_close();transport=public_evidence()
            elif case=="role_invalid":
                other=c.Child();operation="fork_role";result=lib.fr_own_fork(123,512,end,ctypes.byref(other));transport=public_evidence()
            elif case=="deadline_expired":
                other=c.Child();operation="fork_deadline";result=lib.fr_own_fork(123,1,time.monotonic_ns()-1,ctypes.byref(other));transport=public_evidence(other)
            elif case=="session_restart":
                start=c.NativePacket();operation="start_again";result=lib.fr_session_start(123,cap["capsule_sha"].encode("ascii"),ctypes.byref(start));transport=public_evidence()
            elif case=="wait_flags_DATA":result=lib.fr_own_wait(123,ctypes.byref(record),2)
            elif case=="caller_status_DATA":result=lib.fr_fixture_reap(123,record.pid,23<<8)
            else:raise c.Refused("A087_UNIMPLEMENTED_ORDINARY_CASE")
            primary={"operation":operation,"result":result,"record":public_observe(record),"evidence":public_evidence(record),
                "call_DATA":{key:getattr(record,key) for key,_ in record._fields_}}
        for child in children:
            record=child["intention"];state=public_observe(record);stop=None
            if record.pid>0 and (not state["cleanup_reaped"] or not state["handle_closed"]) and case!="close_denied":
                stop=lib.fr_own_stop(123,ctypes.byref(record),min(end,time.monotonic_ns()+10**9))
            child.update(stop_result=stop,final=public_observe(record),final_evidence=public_evidence(record))
            rows.append({key:value for key,value in child.items() if key not in ("intention","read","gate")})
        if not completed:
            other=c.Child();again=lib.fr_own_fork(123,1,end,ctypes.byref(other))
            attempts.append({"operation":"after_refusal_creation","result":again,"created_pid":other.pid})
            c.need(again<0 and other.pid==0,"A087_STICKY_NO_SECOND_CLONE")
        return public_terminal(case,before,rows,attempts,primary,transport,completed)
    finally:
        for child in children:
            state=public_observe(child["intention"])
            if child["intention"].pid>0 and not state["cleanup_reaped"]:
                lib.fr_own_stop(123,ctypes.byref(child["intention"]),min(cap["hard_ns"]-10**9,time.monotonic_ns()+10**9))
        for fd in pipes:os.close(fd)

def public_terminal(case,before,rows,attempts,primary,transport,completed):
    return {"schema":"friday.a087.native-public-result.v1","native_registry_only":True,"case":case,"phase":"SESSION",
        "ordinary_rows":rows,"attempts":attempts,"primary":primary,"transport_evidence":transport,
        "session_before":before,"session_after":observation(),"resources":public_resources(),
        "terminal_completion":completed,"body_complete":False,"acceptance_complete":False,"current_GO":False,
        "all216":"NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION","full_authoritative_registry_controls":"SOURCE_INCOMPLETE","F10_waiver":False}

def controls_inherited_start(raw):
    """Actual native fork/START causal pair; an inherited projection is DATA.
    No replacement native library, numeric PID adoption or owner reset seam.
    """
    data=x.m.inert_json(raw,4096)
    c.need(data=={"schema":"friday.a091.receiving-public-input.v1","case":"inherited_start_owner",
        "effects":"ordinary-own-process-and-plaintext-fd-only"},"A091_OWNER_INPUT")
    cap=c.ADMISSION;lib=c.NATIVE;before=observation();rows=[];children=[];fds=set()
    c.need(before["owner"]==os.getpid() and before["session_ready"]==1 and before["creation_poisoned"]==0,"A091_OWNER_ORDINARY_READY")
    try:
        for role in range(3):
            read,write=os.pipe2(os.O_CLOEXEC);fds.update((read,write));child=c.Child();child.pidfd=-1
            result=lib.fr_own_fork(123,role,cap["work_ns"],ctypes.byref(child))
            if result==0:
                try:
                    os.close(read)
                    def projection():
                        value=c.NativeObservation();c.need(lib.fr_session_projection(ctypes.byref(value))==0,"A091_READONLY_PROJECTION")
                        return {key:getattr(value,key) for key,_ in value._fields_}
                    inherited_before=projection();unchanged_copy=bytes(child);start=c.NativePacket()
                    start_result=lib.fr_session_start(123,cap["capsule_sha"].encode("ascii"),ctypes.byref(start))
                    inherited_after=projection();denied=c.NativeObservation();bound=c.NativeObservation();other=c.Child()
                    result_observe=lib.fr_session_observe(ctypes.byref(denied))
                    result_binding=lib.fr_own_observe(ctypes.byref(child),ctypes.byref(bound))
                    result_creation=lib.fr_own_fork(123,role,cap["work_ns"],ctypes.byref(other))
                    c.need(start_result==result_observe==result_binding==result_creation==-1 and other.pid==0 and bytes(child)==unchanged_copy,"A091_NO_INHERITED_ADOPTION")
                    fixed=("owner","owner_birth","origin","origin_birth","uid","gid","next_sequence")
                    c.need(all(inherited_before[key]==inherited_after[key] for key in fixed) and
                        inherited_before["owner"]==os.getppid() and inherited_before["owner_birth"]==before["owner_birth"] and
                        inherited_before["session_ready"]==inherited_after["session_ready"]==0 and
                        inherited_before["creation_poisoned"]==0 and inherited_after["creation_poisoned"]==1,"A091_EXACT_OWNER_BIRTH_READY_PAIR")
                    receipt={"role":role,"pid":os.getpid(),"parent":os.getppid(),"birth":c.proc(os.getpid())[1],
                        "inherited_before":inherited_before,"inherited_after":inherited_after,"start_result":start_result,
                        "observe_result":result_observe,"binding_result":result_binding,"creation_result":result_creation,
                        "copy_unchanged":True,"new_pid":other.pid,"status_authority":False}
                    message=(json.dumps(receipt,separators=(",",":"))+"\n").encode("ascii")
                    c.need(len(message)<=4096 and os.write(write,message)==len(message),"A091_FINITE_OWN_PIPE_RECEIPT");os._exit(23+role)
                except BaseException:os._exit(125)
            children.append(child);os.close(write);fds.remove(write)
            c.need(result>0 and child.pid==result and child.state==2,"A091_NATIVE_ORDINARY_REGISTERED")
            wait_result=public_wait(child,cap["hard_ns"]-2*10**9)
            message=os.read(read,4097);c.need(0<len(message)<=4096 and message.endswith(b"\n") and message.count(b"\n")==1,"A091_COMPLETE_CHILD_CAUSAL_RECEIPT")
            pair=x.m.inert_json(message,4096);registered=public_observe(child);trace=public_evidence(child)
            release=lib.fr_own_release(ctypes.byref(child));parent_after=observation()
            c.need(wait_result==1 and release==0 and registered["status_known"]==1 and registered["status"]==(23+role)<<8 and
                parent_after["owner"]==before["owner"] and parent_after["owner_birth"]==before["owner_birth"] and
                parent_after["session_ready"]==1 and parent_after["creation_poisoned"]==0,"A091_PARENT_UNCHANGED_ACTUAL_WAIT_ACK")
            rows.append({"role":role,"child":pair,"native":registered,"evidence":trace,"release_result":release,"final":public_observe(child)})
            os.close(read);fds.remove(read)
        return {"schema":"friday.a091.inherited-owner-result.v1","case":data["case"],"session_before":before,
            "session_after":observation(),"rows":rows,"resources":public_resources(),"terminal_completion":True,
            "body_complete":False,"acceptance_complete":False,"GO":False,"all216":"SEPARATE_UNRESOLVED_NOT_RUN"}
    finally:
        for child in children:
            state=public_observe(child)
            if child.pid>0 and not state["cleanup_reaped"]:lib.fr_own_stop(123,ctypes.byref(child),min(cap["hard_ns"]-2*10**9,time.monotonic_ns()+10**9))
        for fd in fds:os.close(fd)

# Ordinary receiving fixtures enter the actual C coordinator at Bootstrap.run.
# A Python coordinator cannot create a second origin or inherit its authority.
def route_receiving_fixture(raw, decoded):
    if type(decoded) is not dict:
        raise c.Refused("A118_RECEIVING_FIXTURE_OBJECT")
    if decoded.get("case") == "inherited_start_owner":
        return controls_inherited_start(raw)
    raise c.Refused("A118_NATIVE_RECEIVING_ENTRY_REQUIRED")

# A158 WHOLE216 exact typed route table. Unsupported historical scenarios are
# abstract obligations only; no constructor, operational payload or execution
# path is supplied for them. Pure normal consumer routes below perform work.
WHOLE216_INPUT_SCHEMA="friday.a158.whole216-input.v1"
WHOLE216_CUSTODY=None  # same-process owned raw/error objects; never a JSON durability claim
WHOLE216_TABLE=[{'position': 0, 'id': 'actual_pinned_registry_browsers_cft_positive', 'expected': {'consumer': 'mapping_check', 'cause': None, 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 1, 'id': 'helper_path', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_HELPER_AND_TAIL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 2, 'id': 'helper_mirror', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_HELPER_AND_TAIL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 3, 'id': 'helper_tail', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_HELPER_AND_TAIL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 4, 'id': 'helper_unknown', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_HELPER_AND_TAIL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 5, 'id': 'platform_value', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_PLATFORM_ROUTE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 6, 'id': 'platform_unknown', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_ALL_PLATFORM_KEYS', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 7, 'id': 'platform_duplicate', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'PLATFORM_DUPLICATE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 8, 'id': 'unknown_fallback', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'PLATFORM_SET_UNKNOWN', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 9, 'id': 'route_tail', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'PLATFORM_VALUE_TAIL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 10, 'id': 'ffmpeg_route', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_PLATFORM_ROUTE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 11, 'id': 'revision', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'BROWSER_REVISION', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 12, 'id': 'version', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'BROWSER_VERSION', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 13, 'id': 'revision_type', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'BROWSER_REVISION', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 14, 'id': 'duplicate_browser', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'BROWSER_REVISION', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 15, 'id': 'browsers_type', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'BROWSER_MAP_TYPE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 16, 'id': 'cft_version', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'CFT_VERSION_TYPE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 17, 'id': 'cft_url', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_URL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 18, 'id': 'cft_platform', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_URL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 19, 'id': 'cft_duplicate_linux', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'EXACT_CFT_URL', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 20, 'id': 'cft_download_type', 'expected': {'consumer': 'mapping_check/tokens/container/route_object', 'cause': 'CFT_DOWNLOAD_TYPE', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 21, 'id': 'owner_pin', 'expected': {'consumer': 'compile_bill', 'cause': 'OWNER_PIN', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 22, 'id': 'helper_digest', 'expected': {'consumer': 'compile_bill', 'cause': 'HELPER_PINS', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 23, 'id': 'browser_route', 'expected': {'consumer': 'compile_bill', 'cause': 'EXACT_BROWSER_ROW', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 24, 'id': 'browser_cap_type', 'expected': {'consumer': 'compile_bill', 'cause': 'EXACT_BROWSER_ROW', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 25, 'id': 'resource_type', 'expected': {'consumer': 'compile_bill', 'cause': 'BILL_LIMITS', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 26, 'id': 'fourth_browser', 'expected': {'consumer': 'compile_bill', 'cause': 'BROWSER_COUNT', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 27, 'id': 'target_change', 'expected': {'consumer': 'compile_bill', 'cause': 'BILL_LIMITS', 'stage': 'compile_bill', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'compile_bill', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 28, 'id': 'real_owner_wrong_digest', 'expected': {'consumer': 'sealed_bytes', 'cause': 'PIN_SHA', 'stage': 'sealed_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 29, 'id': 'real_metadata_wrong_digest', 'expected': {'consumer': 'sealed_bytes', 'cause': 'PIN_SHA', 'stage': 'sealed_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 30, 'id': 'header64KiB_cap', 'expected': {'consumer': 'G1.HeaderReader.readline', 'cause': 'HEADERS_OR_CHUNK_FRAMING_CAP', 'stage': 'G1.HeaderReader.readline', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'header_reader', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 31, 'id': 'mapping_source262KiB_cap', 'expected': {'consumer': 'tokens', 'cause': 'MAPPING_SOURCE_CAP', 'stage': 'tokens', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'tokens', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 32, 'id': 'duplicate_json_key', 'expected': {'consumer': 'G1.inert_json', 'cause': 'JSON_DUPLICATE_KEY', 'stage': 'G1.inert_json', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'inert_json', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 33, 'id': 'nonfinite_json', 'expected': {'consumer': 'G1.inert_json', 'cause': 'JSON_CONSTANT', 'stage': 'G1.inert_json', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'inert_json', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 34, 'id': 'json_depth', 'expected': {'consumer': 'G1.inert_json', 'cause': 'JSON_DEPTH', 'stage': 'G1.inert_json', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'inert_json', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 35, 'id': 'whole3_actual_G1_worker_positive', 'expected': {'consumer': 'execute_core/R4.run_wave/G1.worker', 'cause': None, 'stage': 'network_schedule', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 36, 'id': 'whole3_redirect', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'THREE_BODY_RECEIPTS', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 37, 'id': 'whole3_http404', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'THREE_BODY_RECEIPTS', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 38, 'id': 'whole3_tls_certificate', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:SSLCertVerificationError', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 39, 'id': 'whole3_tls_protocol', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:TLS_EVIDENCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 40, 'id': 'whole3_encoding', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:CONTENT_ENCODING', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 41, 'id': 'whole3_framing', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:HTTP_FRAMING', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 42, 'id': 'whole3_duplicate_length', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:HTTP_FRAMING', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 43, 'id': 'whole3_length_cap', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:DECLARED_BODY_SIZE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 44, 'id': 'whole3_truncated', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:BODY_TRUNCATED', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 45, 'id': 'whole3_hostname', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'TLS_EVIDENCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 46, 'id': 'whole3_verify_mode', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'TLS_EVIDENCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 47, 'id': 'whole3_verified_type', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'TLS_EVIDENCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 48, 'id': 'whole3_cert_digest', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'TLS_EVIDENCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 49, 'id': 'whole3_accounting', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'WORKER_ACCOUNTING_DRIFT', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 50, 'id': 'whole3_digest', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'WORKER_ACCOUNTING_DRIFT', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 51, 'id': 'whole3_acceptance', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'UNPINNED_ACCEPTANCE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 52, 'id': 'whole3_unknown_final', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'WORKER_RECEIPT_INCOMPLETE', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 53, 'id': 'whole3_duplicate_final', 'expected': {'consumer': 'R4.drain/collect/G1.worker', 'cause': 'EVENT_COUNT', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 54, 'id': 'late_input', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_LATE_INPUT_DRIFT', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 55, 'id': 'aggregate_RSS', 'expected': {'consumer': 'R4.run_wave/G1.process_status', 'cause': 'RSS_CAP', 'stage': 'network_schedule', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 56, 'id': 'deadline_create_fresh_browser3', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:create_fresh_browser3', 'stage': 'create_fresh_browser3', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 57, 'id': 'deadline_receipt_write', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:receipt_write', 'stage': 'receipt_write', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 58, 'id': 'deadline_receipts_fsynced', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:receipts_fsynced', 'stage': 'receipts_fsynced', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 59, 'id': 'deadline_inventory_write', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:inventory_write', 'stage': 'inventory_write', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 60, 'id': 'deadline_inventory_fsynced', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:inventory_fsynced', 'stage': 'inventory_fsynced', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 61, 'id': 'deadline_terminal_seal', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:terminal_seal', 'stage': 'terminal_seal', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 62, 'id': 'deadline_closed_terminal', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:closed_terminal', 'stage': 'closed_terminal', 'started_routes': 3, 'hashes': 'stage_dependent', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 63, 'id': 'owner_stop', 'expected': {'consumer': 'Run.guard', 'cause': 'OWNER_STOP', 'stage': 'network_schedule', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 64, 'id': 'sticky_cleanup', 'expected': {'consumer': 'R4.close_fd', 'cause': 'CLEANUP:PRIVATE_CLEANUP_FAULT', 'stage': 'cleanup_descriptor', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 65, 'id': 'unknown_owned_child_sticky', 'expected': {'consumer': 'R4.stop_child/collect', 'cause': 'FORCE_OWNED_CHILD_STOP', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 66, 'id': 'existing_target', 'expected': {'consumer': 'require_absent_target', 'cause': 'EXISTING_TARGET', 'stage': 'require_absent_target', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 67, 'id': 'wrong_mode', 'expected': {'consumer': 'sealed_bytes', 'cause': 'PIN_CUSTODY', 'stage': 'sealed_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 68, 'id': 'hardlink', 'expected': {'consumer': 'sealed_bytes', 'cause': 'PIN_CUSTODY', 'stage': 'sealed_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 69, 'id': 'symlink', 'expected': {'consumer': 'sealed_bytes', 'cause': 'OSError:40', 'stage': 'sealed_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 70, 'id': 'outer_mirror', 'expected': {'consumer': 'mapping_check/container', 'cause': 'EXACT_MIRRORS', 'stage': 'mapping_check', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'mapping_check', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 71, 'id': 'retained_positive', 'expected': {'consumer': 'RetainedTree.check', 'cause': None, 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 72, 'id': 'retained_content', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_LATE_INPUT_DRIFT', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 73, 'id': 'retained_path', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_LATE_INPUT_DRIFT', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 74, 'id': 'retained_fd', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'OSError:9', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 75, 'id': 'retained_directory', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_DIRECTORY_PATH', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 76, 'id': 'retained_membership', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_MEMBERSHIP', 'stage': 'retained_partial_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 77, 'id': 'output_positive', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': None, 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 78, 'id': 'output_mode', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'FILE_CUSTODY', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 79, 'id': 'output_hardlink', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'FILE_CUSTODY', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 80, 'id': 'output_symlink', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'FILE_CUSTODY', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 81, 'id': 'output_membership', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'UNEXPECTED_TREE_ENTRY', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 82, 'id': 'output_root', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'ROOT_CUSTODY', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 83, 'id': 'output_directory', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'DIRECTORY_REPLACED', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 84, 'id': 'output_disk', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'FINAL_DISK_CAP', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 85, 'id': 'output_collision', 'expected': {'consumer': 'BoundedOutput/G1.Output.check/create', 'cause': 'OUTPUT_COLLISION', 'stage': 'output_custody', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 86, 'id': 'reservation', 'expected': {'consumer': 'reservations', 'cause': 'BODY_RESERVATION', 'stage': 'reservations', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'reservations', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 87, 'id': 'file_count', 'expected': {'consumer': 'BoundedOutput.__init__', 'cause': 'OUTPUT_FILE_LIMIT', 'stage': 'BoundedOutput.__init__', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'output_count_refusal', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 88, 'id': 'dir_count', 'expected': {'consumer': 'BoundedOutput.__init__', 'cause': 'OUTPUT_DIRECTORY_LIMIT', 'stage': 'BoundedOutput.__init__', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'output_count_refusal', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 89, 'id': 'pinned_body_file_cap', 'expected': {'consumer': 'R4.guarded_digest', 'cause': 'INPUT_SIZE', 'stage': 'hash_network', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 90, 'id': 'guarded_hash', 'expected': {'consumer': 'R4.guarded_digest', 'cause': 'WALL_TIMEOUT:guarded_hash', 'stage': 'guarded_hash', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 91, 'id': 'guarded_write', 'expected': {'consumer': 'R4.guarded_write', 'cause': 'WALL_TIMEOUT:guarded_write', 'stage': 'guarded_write', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 92, 'id': 'actual_CA_wrong_digest', 'expected': {'consumer': 'ca_context/R4.guarded_digest', 'cause': 'INPUT_SHA', 'stage': 'ca_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 93, 'id': 'actual_CA_parse', 'expected': {'consumer': 'ca_context/SSLContext.load_verify_locations', 'cause': 'NO_CERTIFICATE_OR_CRL_FOUND', 'stage': 'ca_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 94, 'id': 'actual_resources_positive', 'expected': {'consumer': 'G1.resources', 'cause': None, 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 95, 'id': 'actual_resources_leaf', 'expected': {'consumer': 'G1.resources', 'cause': 'CGROUP_LEAF_UNKNOWN', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 96, 'id': 'actual_resources_limit', 'expected': {'consumer': 'G1.resources', 'cause': 'CGROUP_MEMORY_LIMIT_UNKNOWN', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 97, 'id': 'actual_resources_memory', 'expected': {'consumer': 'G1.resources', 'cause': 'CGROUP_MEMORY_SHORTFALL', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 98, 'id': 'actual_resources_ancestor', 'expected': {'consumer': 'G1.resources', 'cause': 'CGROUP_ANCESTOR_MEMORY_SHORTFALL', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 99, 'id': 'actual_resources_disk', 'expected': {'consumer': 'G1.resources', 'cause': 'DISK_SHORTFALL', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 100, 'id': 'actual_resources_host_memory', 'expected': {'consumer': 'G1.resources', 'cause': 'MEMORY_SHORTFALL', 'stage': 'G1.resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 101, 'id': 'whole3_bounded_header', 'expected': {'consumer': 'G1.worker/BoundedResponse/HeaderReader', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:HEADERS_OR_CHUNK_FRAMING_CAP', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 102, 'id': 'late_during_work', 'expected': {'consumer': 'RetainedTree.check', 'cause': 'PARTIAL_LATE_INPUT_DRIFT', 'stage': 'retained_partial_custody', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 103, 'id': 'actual_output_close', 'expected': {'consumer': 'BoundedOutput/G1.Output.close', 'cause': 'OUTPUT_CLOSE:OSError:5', 'stage': 'output_close', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 104, 'id': 'actual_signal_restore', 'expected': {'consumer': 'execute_core/signal.signal', 'cause': 'SIGNAL_RESTORE:OSError:5', 'stage': 'signal_restore', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 105, 'id': 'active_owned_stop', 'expected': {'consumer': 'Run.guard/R4.stop_child', 'cause': 'OWNER_STOP', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 106, 'id': 'public_admission_positive', 'expected': {'consumer': 'public_admission', 'cause': None, 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 107, 'id': 'public_unknown', 'expected': {'consumer': 'public_admission', 'cause': 'EXTERNAL_ROOT_PINS_REQUIRED', 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 108, 'id': 'public_environment', 'expected': {'consumer': 'public_admission', 'cause': 'ISOLATED_ENV', 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 109, 'id': 'public_self_pin', 'expected': {'consumer': 'public_admission', 'cause': 'PIN_SHA:SELF', 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 110, 'id': 'public_bill_pin', 'expected': {'consumer': 'public_admission', 'cause': 'EXTERNAL_BILL_PIN', 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 111, 'id': 'public_controls_pin', 'expected': {'consumer': 'public_admission', 'cause': 'PIN_SHA:CONTROLS', 'stage': 'public_admission', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'public_admission', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 112, 'id': 'deadline_final_retained_hash', 'expected': {'consumer': 'Run.guard/R4.guarded_digest', 'cause': 'WALL_TIMEOUT:final_retained_hash', 'stage': 'final_retained_hash', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 113, 'id': 'deadline_inventory_serialization', 'expected': {'consumer': 'Run.guard/R4.guarded_digest', 'cause': 'WALL_TIMEOUT:inventory_serialization', 'stage': 'inventory_serialization', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 114, 'id': 'deadline_inventory_serialized', 'expected': {'consumer': 'Run.guard/R4.guarded_digest', 'cause': 'WALL_TIMEOUT:inventory_serialized', 'stage': 'inventory_serialized', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 115, 'id': 'terminal_serialization', 'expected': {'consumer': 'terminal_bytes/Run.guard', 'cause': 'WALL_TIMEOUT:terminal_serialization', 'stage': 'terminal_serialization', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 116, 'id': 'terminal_serialized', 'expected': {'consumer': 'terminal_bytes/Run.guard', 'cause': 'WALL_TIMEOUT:terminal_serialized', 'stage': 'terminal_serialized', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 117, 'id': 'serialization_failure', 'expected': {'consumer': 'terminal_bytes', 'cause': 'SERIALIZATION_FAULT', 'stage': 'terminal_serialization', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 118, 'id': 'sticky_serialization', 'expected': {'consumer': 'terminal_bytes', 'cause': 'SERIALIZATION_FAULT', 'stage': 'sticky_uncertainty', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 119, 'id': 'terminal_output_path', 'expected': {'consumer': 'emit_terminal', 'cause': 'TERMINAL_SINK:TERMINAL_OUTPUT_NOT_PIPE', 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 120, 'id': 'connect15_unconnected', 'expected': {'consumer': 'R4.run_wave/stop_child', 'cause': 'REQUEST_TIMEOUT', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 121, 'id': 'request300_connected', 'expected': {'consumer': 'R4.run_wave/stop_child', 'cause': 'REQUEST_TIMEOUT', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 122, 'id': 'work1140_boundary', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:work_boundary', 'stage': 'work_boundary', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 123, 'id': 'hard1200_boundary', 'expected': {'consumer': 'Run.guard', 'cause': 'WALL_TIMEOUT:terminal_reserve', 'stage': 'terminal_reserve', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 124, 'id': 'work1140_active', 'expected': {'consumer': 'R4.run_wave/Run.guard', 'cause': 'WALL_TIMEOUT:network_schedule', 'stage': 'network_schedule', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 125, 'id': 'body_eof_at_cap', 'expected': {'consumer': 'G1.worker/BoundedResponse/write_all', 'cause': None, 'stage': 'hash_network', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 126, 'id': 'body_cap_without_eof', 'expected': {'consumer': 'G1.worker/BoundedResponse/write_all', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:CAP_REACHED_WITHOUT_EOF', 'stage': 'hash_network', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 127, 'id': 'body_short_write', 'expected': {'consumer': 'G1.worker/BoundedResponse/write_all', 'cause': 'SECURITY_INTEGRITY_PROTOCOL:SHORT_WRITE', 'stage': 'hash_network', 'started_routes': 3, 'hashes': 'at_least1', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONTOUR_ABORTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 128, 'id': 'body_partial_write', 'expected': {'consumer': 'G1.worker/BoundedResponse/write_all', 'cause': None, 'stage': 'hash_network', 'started_routes': 3, 'hashes': 'exact3', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 129, 'id': 'disk_reservation_positive', 'expected': {'consumer': 'disk_reservation', 'cause': None, 'stage': 'disk_reservation', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'disk_reservation', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 130, 'id': 'disk_reservation_boundary', 'expected': {'consumer': 'disk_reservation', 'cause': 'RESERVATION', 'stage': 'disk_reservation', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'disk_reservation', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 131, 'id': 'moving_owned_collect_no_hash', 'expected': {'consumer': 'R4.collect', 'cause': 'STOP_UNCONFIRMED', 'stage': 'receipt_write', 'started_routes': 1, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'UNKNOWN_PARTIAL'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 132, 'id': 'combined_active_unknown_close_serialization', 'expected': {'consumer': 'R4.stop_child/G1.Output.close/terminal_bytes', 'cause': 'COMBINED_ACTIVE_UNKNOWN', 'stage': 'event_drain', 'started_routes': 3, 'hashes': 'none', 'owned_children': 3, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 133, 'id': 'terminal_sink_partial_positive', 'expected': {'consumer': 'emit_terminal', 'cause': None, 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMITTED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 134, 'id': 'terminal_sink_zero', 'expected': {'consumer': 'emit_terminal', 'cause': 'TERMINAL_SINK:TERMINAL_SHORT_WRITE', 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 135, 'id': 'terminal_sink_failed', 'expected': {'consumer': 'emit_terminal', 'cause': 'TERMINAL_SINK:OSError:5', 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 136, 'id': 'terminal_sink_blocked', 'expected': {'consumer': 'emit_terminal', 'cause': 'TERMINAL_SINK:TERMINAL_DRAIN_TIMEOUT', 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 137, 'id': 'terminal_sink_partial_failed', 'expected': {'consumer': 'emit_terminal', 'cause': 'TERMINAL_SINK:OSError:5', 'stage': 'terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 138, 'id': 'outer_positive', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': None, 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_BOUNDED_DRAINED_FINISHED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 139, 'id': 'outer_partial', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'PARTIAL_OR_MULTIPLE_TERMINAL', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 140, 'id': 'outer_json', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'JSON_PARSE', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 141, 'id': 'outer_multiple', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'PARTIAL_OR_MULTIPLE_TERMINAL', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 142, 'id': 'outer_false_success', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'INNER_FAILURE', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 143, 'id': 'outer_exit', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'INNER_EXIT', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 144, 'id': 'outer_stderr', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'INNER_STDERR', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 145, 'id': 'outer_stdout_cap', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_PIPE_CAP', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 146, 'id': 'outer_stderr_cap', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_PIPE_CAP', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 147, 'id': 'outer_blocked', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_WORK_TIMEOUT', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 148, 'id': 'outer_rss', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_RSS_CAP', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 149, 'id': 'outer_unknown_member', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'UNKNOWN_CGROUP_MEMBER', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 150, 'id': 'outer_stop_unknown', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_WORK_TIMEOUT', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'STOP_UNCONFIRMED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 151, 'id': 'outer_owner_stop', 'expected': {'consumer': 'supervise_owned/terminal_check', 'cause': 'OUTER_OWNER_STOP', 'stage': 'bounded_drain_reap', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 152, 'id': 'outer_unknown_pin', 'expected': {'consumer': 'Supervisor.seal', 'cause': 'UNKNOWN_PIN', 'stage': 'Supervisor.seal', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 153, 'id': 'outer_runtime_path', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_PATH', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 154, 'id': 'outer_sink_short', 'expected': {'consumer': 'Supervisor.write_terminal', 'cause': 'OUTER_TERMINAL_SHORT_WRITE', 'stage': 'outer_terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 155, 'id': 'outer_sink_failed', 'expected': {'consumer': 'Supervisor.write_terminal', 'cause': 'OSError:5', 'stage': 'outer_terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 156, 'id': 'outer_sink_blocked', 'expected': {'consumer': 'Supervisor.write_terminal', 'cause': 'OUTER_TERMINAL_TIMEOUT', 'stage': 'outer_terminal_sink', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'TERMINAL_EMISSION_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 157, 'id': 'held_positive', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': None, 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 158, 'id': 'held_path_replaced_exact_bytes', 'expected': {'consumer': 'Supervisor.HeldSource.bytes', 'cause': None, 'stage': 'Supervisor.HeldSource.bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 159, 'id': 'held_wrong_sha', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': 'HELD_FD_SHA', 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 160, 'id': 'held_cap', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': 'HELD_FD_CUSTODY', 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 161, 'id': 'held_root_authority_missing', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': 'HELD_ROOT_AUTHORITY', 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 162, 'id': 'held_read_timeout', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': 'HELD_READ_TIMEOUT', 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 163, 'id': 'held_kernel_write_refused', 'expected': {'consumer': 'Supervisor.HeldSource.bytes', 'cause': None, 'stage': 'Supervisor.HeldSource.bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 164, 'id': 'native_launcher_authority_missing', 'expected': {'consumer': 'Supervisor.NativeLaunchAdapter', 'cause': 'HELD_ROOT_AUTHORITY', 'stage': 'Supervisor.NativeLaunchAdapter', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 165, 'id': 'held_unsealed', 'expected': {'consumer': 'Supervisor.bounded_fd_bytes', 'cause': 'HELD_KERNEL_SEALS', 'stage': 'Supervisor.bounded_fd_bytes', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'bounded_fd_bytes', 'effect_domain': 'owned_child_bounded_inert_consumer', 'safety_classification': 'ALLOWLISTED_BOUNDED_INERT_SOURCE'}, {'position': 166, 'id': 'owned_registration_pidfd', 'expected': {'consumer': 'Supervisor.supervise_owned', 'cause': 'OUTER_PIDFD:38', 'stage': 'Supervisor.supervise_owned', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 167, 'id': 'owned_registration_proc', 'expected': {'consumer': 'Supervisor.supervise_owned', 'cause': 'OWNED_PROC_FIXTURE', 'stage': 'Supervisor.supervise_owned', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 168, 'id': 'owned_registration_after_spawn', 'expected': {'consumer': 'Supervisor.supervise_owned', 'cause': 'OWNED_POST_FORK_FIXTURE', 'stage': 'Supervisor.supervise_owned', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 169, 'id': 'owned_registration_race', 'expected': {'consumer': 'Supervisor.supervise_owned', 'cause': 'OWNERSHIP_RACE', 'stage': 'Supervisor.supervise_owned', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 170, 'id': 'owned_registration_post_custody', 'expected': {'consumer': 'Supervisor.supervise_owned', 'cause': 'OWNED_POST_CUSTODY_FIXTURE', 'stage': 'Supervisor.supervise_owned', 'started_routes': 0, 'hashes': 'none', 'owned_children': 1, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'OUTER_FAILED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 171, 'id': 'runtime_positive', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': None, 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 172, 'id': 'runtime_schema', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_SCHEMA', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 173, 'id': 'runtime_file_set', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_FILE_SET', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 174, 'id': 'runtime_stdlib_absent', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_STDLIB_ABSENT', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 175, 'id': 'runtime_directory', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_DIRECTORY_CUSTODY', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 176, 'id': 'runtime_symlink', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_SYMLINK', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 177, 'id': 'runtime_zip', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'UNKNOWN_RUNTIME_ZIP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 178, 'id': 'runtime_preload', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_LIBRARY_PATHS', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 179, 'id': 'runtime_library_path', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_LIBRARY_PATHS', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 180, 'id': 'runtime_membership', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_MEMBERSHIP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 181, 'id': 'runtime_pin_row', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_PIN_ROW', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 182, 'id': 'runtime_read_cap', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_READ_CAP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 183, 'id': 'runtime_unknown_pin', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'UNKNOWN_PIN', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 184, 'id': 'runtime_pin_sha', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'HELD_FD_SHA', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 185, 'id': 'runtime_pin_custody', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'PIN_CUSTODY', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 186, 'id': 'runtime_soname_map', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_SONAME_MAP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 187, 'id': 'runtime_cache_format', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_CACHE_FORMAT', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 188, 'id': 'runtime_cache_cap', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_CACHE_CAP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 189, 'id': 'runtime_cache_string', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_CACHE_STRING', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 190, 'id': 'runtime_cache_resolution', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_CACHE_RESOLUTION', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 191, 'id': 'runtime_interpreter', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_INTERPRETER_ELF', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 192, 'id': 'runtime_elf_format', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_ELF_FORMAT', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 193, 'id': 'runtime_elf_headers', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_ELF_HEADERS', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 194, 'id': 'runtime_dynamic_cap', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_DYNAMIC_CAP', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 195, 'id': 'runtime_dynamic_search', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_DYNAMIC_SEARCH', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 196, 'id': 'runtime_strtab', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_STRTAB', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 197, 'id': 'runtime_soname', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_SONAME', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 198, 'id': 'runtime_dependency', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'UNKNOWN_RUNTIME_DEPENDENCY', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 199, 'id': 'runtime_loader_pin', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_LOADER_PIN', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 200, 'id': 'runtime_loader_custody', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'RUNTIME_LOADER_CUSTODY', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 201, 'id': 'runtime_timeout', 'expected': {'consumer': 'Supervisor.runtime_preflight', 'cause': 'PREFLIGHT_TIMEOUT', 'stage': 'Supervisor.runtime_preflight', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 202, 'id': 'resource_envelope_positive', 'expected': {'consumer': 'acquisition_resources', 'cause': None, 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 203, 'id': 'resource_envelope_membership', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_CGROUP_MEMBERSHIP', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 204, 'id': 'resource_envelope_mount', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_CGROUP_MOUNT', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 205, 'id': 'resource_envelope_envelope', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_KERNEL_ENVELOPE', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 206, 'id': 'resource_envelope_memory', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_INNER_MEMORY', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 207, 'id': 'resource_envelope_ancestor', 'expected': {'consumer': 'acquisition_resources', 'cause': 'CGROUP_ANCESTOR_MEMORY_SHORTFALL', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 208, 'id': 'resource_envelope_host', 'expected': {'consumer': 'acquisition_resources', 'cause': 'MEMORY_SHORTFALL', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 209, 'id': 'resource_envelope_disk', 'expected': {'consumer': 'acquisition_resources', 'cause': 'DISK_SHORTFALL', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 210, 'id': 'resource_envelope_cpu', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_CPU_AFFINITY', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 211, 'id': 'resource_envelope_cap', 'expected': {'consumer': 'acquisition_resources', 'cause': 'RESOURCE_READ_CAP', 'stage': 'acquisition_resources', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 212, 'id': 'cgroup_values_positive', 'expected': {'consumer': 'Supervisor.cgroup_values', 'cause': None, 'stage': 'Supervisor.cgroup_values', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'CONSUMER_ACCEPTED_NO_ADMISSION'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 213, 'id': 'cgroup_values_bound', 'expected': {'consumer': 'Supervisor.cgroup_values', 'cause': 'CGROUP_BOUND_UNKNOWN', 'stage': 'Supervisor.cgroup_values', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 214, 'id': 'cgroup_values_populated', 'expected': {'consumer': 'Supervisor.cgroup_values', 'cause': 'CGROUP_NOT_EXCLUSIVE_EMPTY', 'stage': 'Supervisor.cgroup_values', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}, {'position': 215, 'id': 'cgroup_values_events', 'expected': {'consumer': 'Supervisor.cgroup_values', 'cause': 'CGROUP_NOT_EXCLUSIVE_EMPTY', 'stage': 'Supervisor.cgroup_values', 'started_routes': 0, 'hashes': 'none', 'owned_children': 0, 'minimum_consumer_calls': 1, 'forbidden_effects': {'network': 0, 'exec': 0, 'original_write': 0}, 'state': 'REFUSED'}, 'active': True, 'producer': 'a171_actual', 'effect_domain': 'original_bounded_owned_operands_actual_consumer_or_explicit_code_gap', 'safety_classification': 'BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED'}]

def whole216_input(raw,plan):
    data=x.m.inert_json(raw,1048576)
    c.need(type(data) is dict and set(data)=={"schema","position","id","generation","producer","arguments"} and
        data["schema"]==WHOLE216_INPUT_SCHEMA and type(data["position"]) is int and 0<=data["position"]<216 and
        type(data["generation"]) is int and data["generation"]==1 and type(data["arguments"]) is dict,
        "A158_TYPED_PER_ID_INPUT")
    spec=WHOLE216_TABLE[data["position"]]
    c.need(data["id"]==spec["id"] and data["producer"]==spec["producer"] and
        list(plan["control_map"])[data["position"]]==spec["id"] and
        plan["control_map"][spec["id"]]==spec["expected"],"A158_EXACT_ID_ORDER_AND_ORIGINAL_EXPECTATION")
    c.need(raw==(json.dumps(data,separators=(",",":"))+"\n").encode("ascii"),"A158_CANONICAL_ROOT_SELECTED_TYPED_BYTES")
    a171_validate_input(data,spec["expected"],c.need)
    return data,spec

def whole216_hex(value,cap):
    c.need(type(value) is str and len(value)<=2*cap and len(value)%2==0 and
        all(ch in "0123456789abcdef" for ch in value),"A158_INERT_BYTE_ARGUMENT")
    return bytes.fromhex(value)

def whole216_owned_read(args,custody):
    domain=args.get("domain")
    common={"domain","sha256","cap","sealed","root","expired"}
    c.need(type(args.get("sha256")) is str and len(args["sha256"])==64 and
        all(ch in "0123456789abcdef" for ch in args["sha256"]) and
        type(args.get("cap")) is int and 0<=args["cap"]<=1048576 and
        all(type(args.get(key)) is bool for key in ("sealed","root","expired")),"A158_BOUNDED_READ_TYPED_ARGUMENTS")
    owned={"fd":None,"owner":os.getpid(),"identity9":None,"close_attempted":False,
        "closed":False,"close_error":None,"original_error":None}
    fd=None;primary=None
    if domain=="source":
        c.need(set(args)==common|{"source_role"} and args["source_role"] in s.SOURCE_ROLES,
            "A158_EXISTING_ROOT_SELECTED_SOURCE_ROLE")
        fd=s.SOURCE_ROLES[args["source_role"]][0]  # held borrowed Source FD is never closed here
    else:
        c.need(domain=="owned_inert" and set(args)==common|{"raw_hex","seal_owned"} and
            type(args["seal_owned"]) is bool,"A158_OWNED_INERT_READ_DOMAIN")
        raw=whole216_hex(args["raw_hex"],262144)
    try:
        if domain=="owned_inert":
            custody.append(owned)  # guard exists before first actual FD
            fd=os.memfd_create("friday-a158-owned-inert",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
            owned["fd"]=fd;owned["identity9"]=[str(v) for v in x.identity(os.fstat(fd))]
            view=memoryview(raw)
            while view:
                c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A158_INERT_METADATA_ORIGINAL_CLOCK")
                n=os.write(fd,view);c.need(n>0,"A158_INERT_METADATA_WRITE");view=view[n:]
            if args["seal_owned"]:s.fcntl.fcntl(fd,s.fcntl.F_ADD_SEALS,s.SEALS)
        value=a171_called("Supervisor.bounded_fd_bytes","Supervisor.bounded_fd_bytes",s.bounded_fd_bytes,fd,args["sha256"],args["cap"],sealed=args["sealed"],root=args["root"],
            deadline=time.monotonic()-1 if args["expired"] else c.ADMISSION["work_ns"]/10**9)
        return {"consumer_bytes":len(value),"consumer_sha256":hashlib.sha256(value).hexdigest(),
            "actual_read_domain":domain,"borrowed_Source_closed":False}
    except BaseException as exc:primary=exc;raise
    finally:
        if owned["fd"] is not None:
            owned["close_attempted"]=True
            try:os.close(owned["fd"])
            except BaseException as exc:
                owned["close_error"]=x.cause(exc);owned["original_error"]=exc
                if primary is None:raise
            else:owned["closed"]=True

def whole216_perform(data,spec,plan,custody):
    # This dispatch contains only actual fixed approved pure consumers. Names
    # in the original historical bill never become a callable or a producer.
    args=data["arguments"];route=spec["producer"]
    if route=="a171_actual":return a171_perform(data,spec,plan,custody)
    if route=="mapping_check":
        c.need(set(args)=={"documents"} and type(args["documents"]) is dict and
            set(args["documents"])=={"metadata/playwright/browsers.json","metadata/playwright/cft-version.json",
                "metadata/playwright/registry-index.ts"},"A158_MAPPING_ARGUMENTS")
        docs={name:whole216_hex(value,524288 if name.endswith(".ts") else 65536)
            for name,value in args["documents"].items()}
        result=a171_called("mapping_check","mapping_check",x.mapping_check,docs,plan["browser_archives"])
        if data["id"]=="actual_pinned_registry_browsers_cft_positive":
            remote,paths,dirs=x.compile_bill(plan)
            x.disk_reservation(remote,len(x.HELD_BYTES[x.BILL]))
            x.reservations(remote,x.HELD_BYTES[x.BILL],paths,dirs)
            held_positive=whole216_owned_read({"domain":"source","source_role":"executor",
                "sha256":c.ADMISSION["pins"]["executor"],"cap":1048576,"sealed":True,"root":True,"expired":False},custody)
            public_positive=x.public_admission(["friday-reviewed-executor","--preflight-reviewed-a061",
                c.ADMISSION["pins"]["executor"],c.ADMISSION["pins"]["bill"],c.ADMISSION["pins"]["controls"]],
                x.ENV,s.sys.flags,x.STEM+"-EXECUTOR.py")
            with io.BytesIO(b"a158-owned-inert-metadata\n") as positive_stream:
                header_positive=x.m.HeaderReader(positive_stream);header_positive.readline()
            result={"mapping":result,"positive_prerequisites":{"compile_bill_return":(remote,paths,dirs),
                "reservations_returned":True,"disk_reservation_returned":True,
                "bounded_fd_read":held_positive,"public_admission_mode":public_positive[0],
                "header_reader_bytes":header_positive.count,
                "inert_json_already_consumed_actual_metadata":True,"tokens_already_consumed_actual_metadata":True}}
        return result
    if route=="compile_bill":
        c.need(set(args)=={"plan"} and type(args["plan"]) is dict,"A158_BILL_ARGUMENTS")
        return a171_called("compile_bill","compile_bill",x.compile_bill,args["plan"])
    if route=="tokens":
        c.need(set(args)=={"raw_hex"},"A158_TOKEN_ARGUMENTS")
        tokens=a171_called("tokens","tokens",x.tokens,whole216_hex(args["raw_hex"],524288))
        return {"token_count":len(tokens)}
    if route=="inert_json":
        c.need(set(args)=={"raw_hex"},"A158_JSON_ARGUMENTS")
        value=a171_called("G1.inert_json","G1.inert_json",x.m.inert_json,whole216_hex(args["raw_hex"],262144),262144)
        return {"parsed_type":type(value).__name__}
    if route=="header_reader":
        c.need(set(args)=={"raw_hex"},"A158_HEADER_READER_ARGUMENTS")
        # Actual unchanged bounded line parser on owned inert bytes; no
        # network response, socket/TLS claim or unsafe request fixture.
        raw=whole216_hex(args["raw_hex"],65537)
        with io.BytesIO(raw) as stream:
            reader=x.m.HeaderReader(stream);lines=0
            while True:
                c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A158_HEADER_ORIGINAL_CLOCK")
                line=a171_called("G1.HeaderReader.readline","G1.HeaderReader.readline",reader.readline);lines+=bool(line)
                if not line:break
            return {"actual_header_bytes":reader.count,"actual_line_count":lines,"network":False}
    if route=="public_admission":
        c.need(set(args)=={"argv","environment"} and type(args["argv"]) is list and
            len(args["argv"])<=8 and all(type(v) is str and len(v)<=512 for v in args["argv"]) and
            type(args["environment"]) is dict and len(args["environment"])<=8 and
            all(type(k) is str and type(v) is str and len(k)<=64 and len(v)<=512
                for k,v in args["environment"].items()),"A158_PUBLIC_PURE_ARGUMENTS")
        value=a171_called("public_admission","public_admission",x.public_admission,args["argv"],args["environment"],s.sys.flags,x.STEM+"-EXECUTOR.py")
        return {"consumer_returned":True,"mode":value[0] if value is not None else None,
            "actual_bill_size":len(value[1]) if value is not None else None,
            "actual_bill_sha256":hashlib.sha256(value[1]).hexdigest() if value is not None else None,
            "Source_issued_admission":False}
    if route=="bounded_fd_bytes":
        return whole216_owned_read(args,custody)
    if route=="output_count_refusal":
        c.need(set(args)=={"paths","dirs"} and all(type(args[key]) is list and len(args[key])<=64 and
            all(type(v) is str and len(v)<=64 for v in args[key]) for key in ("paths","dirs")),
            "A158_COUNT_ONLY_ARGUMENTS")
        # These two negative-only IDs call the real unchanged initial count
        # predicates, but cannot reach Output/create or filesystem effects.
        c.need((data["id"]=="file_count" and len(args["paths"])>plan["limits"]["regular_files_max"]) or
            (data["id"]=="dir_count" and len(args["paths"])<=plan["limits"]["regular_files_max"] and
                len(args["dirs"])+1>plan["limits"]["directories_max"]),"A158_BEFORE_OUTPUT_CREATION_ONLY")
        a171_called("BoundedOutput.__init__","BoundedOutput.__init__",x.BoundedOutput,None,args["paths"],args["dirs"],None)
        raise c.Refused("A158_COUNT_CONSUMER_UNEXPECTED_RETURN")
    if route in ("disk_reservation","reservations"):
        wanted={"remote","metadata_bytes"} if route=="disk_reservation" else {"remote","raw_hex","paths","dirs"}
        c.need(set(args)==wanted and type(args["remote"]) is list and len(args["remote"])==3 and
            all(type(row) is dict and set(row)=={"cap"} and type(row["cap"]) is int and
                0<=row["cap"]<=2147483648 for row in args["remote"]),"A158_RESERVATION_ARGUMENTS")
        if route=="disk_reservation":
            c.need(type(args["metadata_bytes"]) is int and 0<=args["metadata_bytes"]<=2147483648,"A158_RESERVATION_SIZE")
            a171_called("disk_reservation","disk_reservation",x.disk_reservation,args["remote"],args["metadata_bytes"])
        else:
            c.need(type(args["paths"]) is list and type(args["dirs"]) is list and
                len(args["paths"])<=64 and len(args["dirs"])<=64 and
                all(type(v) is str and len(v)<=512 for v in args["paths"]+args["dirs"]),"A158_RESERVATION_NAMES")
            a171_called("reservations","reservations",x.reservations,args["remote"],whole216_hex(args["raw_hex"],262144),args["paths"],args["dirs"])
        return {"consumer_returned":True}
    raise c.Refused("A158_REQUIRED_NOT_RUN_NO_SAFE_PERFORMING_PRODUCER")

def whole216_producer(data,spec,plan):
    global A171_OBSERVATION, A171_RAW_FRAMES
    A171_RAW_FRAMES=[]
    A171_OBSERVATION=a171_new_observation()
    if spec["expected"]["consumer"] in ("G1.resources","acquisition_resources"):
        raise A171CodeGap("RESOURCE_OBSERVER_MUST_PRECEDE_DELEGATED_JOIN:"+spec["expected"]["consumer"])
    lib=c.NATIVE;joined=c.NativeEvidence();finished=c.NativeEvidence()
    c.need(lib.fr_controller_join(data["position"],ctypes.byref(joined))==0,"A158_GENUINE_ROOT_SELECTED_DELEGATION")
    projection=c.NativeObservation()
    c.need(lib.fr_controller_observe(ctypes.byref(projection))==0,"A158_ACTUAL_DELEGATED_PROJECTION")
    parent,birth=c.proc(os.getpid())
    c.need(parent==joined.expected.owner and birth==joined.expected.birth and
        joined.expected.pid==os.getpid() and joined.expected.detail==data["position"]+1 and
        joined.observed_pid==projection.origin and joined.observed_uid==joined.observed_gid==0,
        "A158_ACTUAL_PARENT_ROLE_GENERATION_ROOT_ORIGIN")
    # Actual clock, limits, cgroup/ancestor/memory/affinity observations; no
    # whole-system RAM/implicit-I/O value is invented from those observations.
    resources=None
    result=None;primary=None;outcome="RETURNED";custody=[];operation_error=None
    try:
        resources=c.production_resources(c.ADMISSION)
        A171_OBSERVATION["resource_precondition"]={"passed":True,"actual":resources,"joined_delegated_producer":True}
        result=whole216_perform(data,spec,plan,custody)
    except BaseException as exc:
        operation_error=exc;outcome="REFUSED"
        if isinstance(exc,A171CodeGap):
            A171_OBSERVATION["unavailable_reason"]=str(exc)
            primary="A171_CODE_GAP:"+str(exc)
        else:
            fault=A171_OBSERVATION["first_fault"]
            primary=fault["cause"] if fault is not None else a171_cause(exc)
            if fault is None:
                A171_OBSERVATION["first_fault"]={"cause":primary,"stage":"producer_precondition",
                    "consumer":None,"at_ns":str(time.monotonic_ns())}
        if resources is None:A171_OBSERVATION["resource_precondition"]={"passed":False,"cause":primary}
    if primary is None and A171_OBSERVATION["first_fault"] is not None:
        primary=A171_OBSERVATION["first_fault"]["cause"];outcome="REFUSED"
    if A171_OBSERVATION["state"] is None:
        A171_OBSERVATION["state"]="REFUSED" if primary is not None else "CONSUMER_ACCEPTED_NO_ADMISSION"
    A171_OBSERVATION["descriptor_records"]=[{key:value for key,value in record.items() if key!="original_error"} for record in custody]
    if any(record["fd"] is not None and not record["closed"] for record in custody):
        A171_OBSERVATION["state"]="STOP_UNCONFIRMED"
        A171_OBSERVATION["facts"]["auxiliary_close_unconfirmed"]=True
        primary=primary or "A171_OPERATION_AUXILIARY_CLOSE_UNCONFIRMED"
        outcome="REFUSED"
    # This FINISH acknowledges only the selected ordinary producer. It does
    # not reap itself or reset the inherited coordinator's session/sequence.
    c.need(lib.fr_controller_finish(ctypes.byref(finished))==0,"A158_ACTUAL_CONTROLLER_FINISH_ACK")
    after=c.NativeObservation()
    c.need(lib.fr_controller_observe(ctypes.byref(after))==0,"A158_CONTROLLER_FINAL_PROJECTION")
    return {"schema":"friday.a158.performing-producer-result.v1","id":data["id"],"position":data["position"],
        "generation":1,"producer":spec["producer"],"operation_outcome":outcome,"actual_cause":primary,
        "consumer_result":result,"operation_observation":A171_OBSERVATION,"operation_error_DATA":None if operation_error is None else {"type":type(operation_error).__name__,"cause":primary,"errno":getattr(operation_error,"errno",None),"original_object_lifetime":"same_producer_until_actual_exit"},"actual_actor":{"pid":os.getpid(),"parent":parent,"birth":birth,
            "uid":os.getuid(),"gid":os.getgid()},"Root_selected_start":c.evidence_DATA(joined),
        "Root_selected_finish":c.evidence_DATA(finished),
        "delegation_before":{key:getattr(projection,key) for key,_ in projection._fields_},
        "delegation_after":{key:getattr(after,key) for key,_ in after._fields_},
        "resources":resources,"operation_auxiliary_cleanup":[{k:v for k,v in record.items() if k!="original_error"} for record in custody],
        "whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN",
        "wait_status_authority":False,"body_complete":False,"acceptance_complete":False,"SourceReady":False,"GO":False}

def a171_unsafe_historical(case, consumer, cause):
    # Exact-id membership only. The benign exact29 obligations are not in this
    # set, and a shared token is not an unsafe classification. This function
    # selects no operational body.
    return False

def a171_block_child():
    pid=os.fork()
    if pid==0:
        try:
            while True: time.sleep(60)
        except BaseException:
            os._exit(125)
        os._exit(125)
    return pid

def a171_consume_retirement(journal, owned_rows, cleanup):
    hard_ns = time.monotonic_ns() + 1000000000
    if c.ADMISSION is not None:
        hard_ns = min(hard_ns, c.ADMISSION["hard_ns"])
    by_pid = {}
    for row in owned_rows or []:
        if type(row) is dict and type(row.get("pid")) is int:
            by_pid[row["pid"]] = row
    for rec in journal:
        row = by_pid.get(rec["pid"])
        if rec["owner"] != "supervisor":
            rec["errors"].append("RETIREMENT_OWNER_UNEXPECTED")
            cleanup.append(rec["errors"][-1])
            continue
        if row is None:
            rec["retirement"] = "no_row_no_signal"
            rec["errors"].append("RETIREMENT_ROW_ABSENT")
            cleanup.append(rec["errors"][-1])
            continue
        if row.get("reaped") is True or row.get("retirement_consumed") is True:
            rec["reaped"] = True
            rec["status"] = row.get("status")
            rec["retirement"] = "supervisor_consumed_once"
            continue
        while time.monotonic_ns() < hard_ns and not rec["reaped"]:
            try:
                done, value = os.waitpid(rec["pid"], os.WNOHANG)
            except ChildProcessError:
                rec["errors"].append("RETIREMENT_WAIT:ChildProcessError")
                cleanup.append(rec["errors"][-1])
                break
            except OSError as exc:
                rec["errors"].append("RETIREMENT_WAIT:" + str(exc.errno))
                cleanup.append(rec["errors"][-1])
                break
            if done:
                rec["reaped"] = True
                rec["status"] = value
                rec["retirement"] = "supervisor_wait_collected_once"
                break
            select.select([], [], [], 0.01)
        if not rec["reaped"]:
            rec["retirement"] = "finite_end_unconfirmed"
            rec["errors"].append("RETIREMENT_FINITE_END_UNCONFIRMED")
            cleanup.append(rec["errors"][-1])


def a171_complete_stream(raw, label):
    c.need(type(raw) is bytes and len(raw) <= 1048576, "A171_COMPLETE_STREAM_BOUND")
    A171_OBSERVATION["operation_raw"].append({
        "label": label, "raw_hex": raw.hex(), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "hash_only": False,
        "transport": "complete_bytes"})

def a171_outer_child(case, writeout, writeerr):
    if case=="outer_positive":
        payload={"state":"CONTROLS_FINISHED_REQUIRES_INDEPENDENT_CAUSAL_RECEIPT_REVIEW",
            "network_effects":0,"production_target_created":False,"production_admission":False,"controls":[]}
        os.write(writeout,(json.dumps(payload,separators=(",",":"))+"\n").encode("ascii"))
        os._exit(0)
    if case=="outer_partial":
        os.write(writeout,b"partial-without-newline");os._exit(0)
    if case=="outer_multiple":
        os.write(writeout,b'{"state":"NO"}\n{"state":"NO"}\n');os._exit(0)
    if case=="outer_json":
        os.write(writeout,b"not-json\n");os._exit(0)
    if case=="outer_false_success":
        os.write(writeout,b'{"state":"NO"}\n');os._exit(0)
    if case=="outer_exit":
        os._exit(1)
    if case=="outer_stderr":
        os.write(writeerr,b"x");os._exit(0)
    if case in ("outer_stdout_cap","outer_stderr_cap"):
        blob=b"x"*(1048576+1);view=memoryview(blob);fd=writeout if case=="outer_stdout_cap" else writeerr
        while view:
            count=os.write(fd,view)
            if count<=0: os._exit(1)
            view=view[count:]
        os._exit(0)
    if case in ("outer_blocked","outer_stop_unknown","outer_rss","outer_unknown_member","outer_owner_stop"):
        while True: time.sleep(30)
    os._exit(125)

def a171_lookup_write_all():
    bases = [x]
    for extra in (getattr(x, "G1", None), getattr(x, "m", None), getattr(x, "r", None)):
        if extra is not None:
            bases.append(extra)
    seen = []
    for base in bases:
        if base is None or any(base is item for item in seen):
            continue
        seen.append(base)
        fn = getattr(base, "write_all", None)
        if fn is not None:
            return fn
        for owner in (getattr(base, "BoundedResponse", None), getattr(base, "worker", None)):
            if owner is None:
                continue
            fn = getattr(owner, "write_all", None)
            if fn is not None:
                return fn
            nested = getattr(owner, "BoundedResponse", None)
            if nested is not None and getattr(nested, "write_all", None) is not None:
                return nested.write_all
    return None

def a171_local_pipe_facts(case, observation):
    # Local pipe evidence only. It is not G1.worker/BoundedResponse/write_all.
    read_fd,write_fd=os.pipe2(os.O_CLOEXEC)
    try:
        if case=="body_eof_at_cap":
            os.write(write_fd,b"x"*1024);os.close(write_fd);write_fd=None
            got=os.read(read_fd,2048)
            observation["facts"]["actual_pipe_bytes"]=len(got)
            observation["facts"]["actual_pipe_eof"]=os.read(read_fd,1)==b""
        elif case=="body_partial_write":
            os.set_blocking(write_fd,False)
            blob=b"y"*(1048576+1);wrote=0
            try:
                while wrote<len(blob):
                    count=os.write(write_fd,blob[wrote:wrote+65536])
                    if count<=0: break
                    wrote+=count
            except BlockingIOError:
                observation["facts"]["actual_pipe_blockingio"]=True
            observation["facts"]["actual_pipe_bytes"]=wrote
    finally:
        if write_fd is not None: os.close(write_fd)
        os.close(read_fd)

def a171_coordinator_perform(data, spec, plan, journal, sink):
    case = data["id"]
    expected = spec["expected"]
    consumer = expected["consumer"]
    stage = expected["stage"]
    cause = expected["cause"]
    observation = A171_OBSERVATION
    observation["performer_entries"] += 1
    if a171_unsafe_historical(case, consumer, cause):
        observation["unavailable_reason"] = "ABSTRACT_REQUIRED_NOT_RUN:" + case
        return

    def own(pid):
        journal.append({"pid": pid, "owner": "supervisor", "reaped": False, "status": None,
                        "retirement": None, "errors": []})
        return pid

    if case.startswith("owned_registration_") or case.startswith("outer_"):
        mode = "--preflight" if case == "outer_false_success" else "--controls"
        kwargs = {}
        if case == "outer_owner_stop":
            kwargs["cancel"] = lambda: True
        if case == "outer_rss":
            kwargs["rss"] = lambda: s.RSS + 1
        if case == "outer_unknown_member":
            kwargs["members"] = lambda: [os.getpid()]
        if case == "outer_stop_unknown":
            kwargs["stop"] = lambda pid, rec: False

        def spawn(writeout, writeerr, direct, case=case):
            pid = os.fork()
            if pid == 0:
                try:
                    if case.startswith("owned_registration_"):
                        os._exit(0)
                    a171_outer_child(case, writeout, writeerr)
                except BaseException:
                    os._exit(125)
                os._exit(125)
            direct["pid"] = pid
            own(pid)
            return pid

        result = a171_called(consumer, stage, s.supervise_owned, spawn, wall=3, reserve=2,
                             terminal_reserve=1, mode=mode, expected_controls={}, **kwargs)
        sink["owned"] = result["owned"]
        a171_complete_stream(result["stdout"], "outer_stdout")
        a171_complete_stream(result["stderr"], "outer_stderr")
        observation["state"] = result["state"]
        observation["target_children"] = [{"pid": row["pid"],
                                           "lifecycle": "REAPED" if row["reaped"] else "UNREAPED",
                                           "connected": True} for row in result["owned"]]
        if result["reason"] is not None and observation["first_fault"] is None:
            observation["first_fault"] = {"cause": result["reason"], "stage": stage, "consumer": consumer,
                                          "at_ns": str(time.monotonic_ns())}
        observation["facts"]["supervisor_errors"] = list(result["errors"])
        observation["facts"]["fixture_cause_manufactured"] = False
        return
    if case.startswith("deadline_"):
        observation["facts"]["actual_stage_hash_relation"] = False
        observation["facts"]["hash_relation"] = "NOT_MANUFACTURED"
        run = a171_run()
        try:
            a171_called(consumer, stage, run.guard, stage, False)
        finally:
            a171_merge_run(run)
        return
    if case.startswith("body_"):
        fn = a171_lookup_write_all()
        observation["facts"]["write_all_symbol"] = fn is not None
        if fn is None:
            observation["unavailable_reason"] = "ORIGINAL_WRITE_ALL_ABSENT:" + case
            return
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            blob = b"owned-inert-bounded"
            a171_called(consumer, stage, fn, write_fd, blob)
            os.close(write_fd)
            write_fd = None
            got = b""
            while True:
                part = os.read(read_fd, 65536)
                if not part:
                    break
                got += part
                c.need(len(got) <= 1048576, "A171_COMPLETE_STREAM_BOUND")
            observation["facts"]["actual_write_all_bytes"] = len(got)
            observation["facts"]["actual_write_all_eof"] = True
            a171_complete_stream(got, "write_all_body")
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
        observation["facts"]["historical_three_worker_body"] = "ABSTRACT_REQUIRED_NOT_RUN"
        return
    observation["facts"]["token_classifier"] = "not_used"
    observation["facts"]["historical_operational_body"] = "ABSTRACT_REQUIRED_NOT_RUN"
    observation["unavailable_reason"] = "HISTORICAL_OPERATIONAL_BODY_ABSTRACT_REQUIRED_NOT_RUN:" + case

def a171_coordinator_receipt(data, spec, plan, cap):
    global A171_OBSERVATION, A171_RAW_FRAMES
    A171_RAW_FRAMES = []
    A171_OBSERVATION = a171_new_observation()
    A171_OBSERVATION["resource_precondition"] = {"domain": "coordinator_not_delegated_producer",
        "passed": True, "entered_inner_leaf": False, "joined_delegated_producer": False}
    journal = []
    cleanup = []
    before = None
    after = None
    primary = None
    original_error = None
    passed = False
    outcome = None
    actual_cause = None
    sink = {}
    try:
        before = observation()
    except BaseException as exc:
        cleanup.append(x.cause(exc))
    try:
        a171_coordinator_perform(data, spec, plan, journal, sink)
        fault = A171_OBSERVATION["first_fault"]
        actual_cause = None if fault is None else fault["cause"]
        outcome = "RETURNED" if actual_cause is None else "REFUSED"
        if A171_OBSERVATION["unavailable_reason"] is None and A171_OBSERVATION["state"] is None:
            A171_OBSERVATION["state"] = "REFUSED" if actual_cause is not None else "CONSUMER_ACCEPTED_NO_ADMISSION"
        a171_validate_observation(A171_OBSERVATION, spec["expected"], c.need, data["id"])
        passed = True
    except BaseException as exc:
        primary = x.cause(exc)
        original_error = exc
        passed = False
        fault = A171_OBSERVATION["first_fault"]
        if actual_cause is None and fault is not None:
            actual_cause = fault["cause"]
            outcome = "REFUSED"
    finally:
        a171_consume_retirement(journal, sink.get("owned"), cleanup)
    try:
        after = observation()
    except BaseException as exc:
        cleanup.append(x.cause(exc))
    streams = [item for item in A171_OBSERVATION["operation_raw"]
               if item.get("label") in ("outer_stdout", "outer_stderr", "write_all_body")]
    producer = {"schema": "friday.a158.performing-producer-result.v1", "id": data["id"],
                "position": data["position"], "generation": 1, "producer": spec["producer"],
                "delegated_fork": False, "operation_outcome": outcome, "actual_cause": actual_cause,
                "operation_observation": A171_OBSERVATION, "body_complete": False,
                "acceptance_complete": False, "SourceReady": False, "GO": False}
    return {"schema": "friday.a158.whole216-controller-result.v1", "id": data["id"],
            "position": data["position"], "generation": 1, "producer_called": len(journal) > 0,
            "child_created": len(journal) > 0, "producer": producer, "producer_streams": streams,
            "actual_auxiliary_cleanup": [{"pid": rec["pid"], "reaped": rec["reaped"],
                                          "retirement": rec["retirement"], "errors": list(rec["errors"])}
                                         for rec in journal],
            "primary_error": primary, "original_error": original_error, "cleanup_errors": cleanup,
            "session_before": before, "session_after": after, "native_child": None, "native_evidence": None,
            "operation_outcome": outcome, "actual_cause": actual_cause, "passed": passed,
            "disposition": "SOURCE_CAUSAL_ROUTE_OBSERVED_NO_WHOLE_ADMISSION" if passed else "REFUSED_SCOPED_RECEIPT",
            "terminal_completion": passed, "body_complete": False, "acceptance_complete": False,
            "SourceReady": False, "GO": False, "whole216_credit": False, "F10_waiver": False,
            "whole_assignment_RAM_and_implicit_IO": "UNKNOWN_NOT_ZERO_NOT_PROVEN"}

def a171_restore_raw_frames(producer, trailer):
    obs=producer.get("operation_observation") if type(producer) is dict else None
    rows=obs.get("operation_raw") if type(obs) is dict else None
    if type(rows) is not list:
        c.need(trailer==b"","A171_FULL_RAW_FRAME_NOT_RESTORED");return
    rest=trailer
    for item in rows:
        if type(item) is dict and item.get("transport")=="stdout_frame":
            c.need(len(rest)>=4,"A171_FULL_RAW_FRAME_NOT_RESTORED")
            size=int.from_bytes(rest[:4],"big")
            c.need(size==item.get("bytes") and 0<=size<=1048576 and len(rest)>=4+size,"A171_FULL_RAW_FRAME_NOT_RESTORED")
            blob=bytes(rest[4:4+size])
            c.need(hashlib.sha256(blob).hexdigest()==item.get("sha256"),"A171_FULL_RAW_FRAME_NOT_RESTORED")
            item["raw_hex"]=blob.hex();rest=rest[4+size:]
    c.need(rest==b"","A171_FULL_RAW_FRAME_NOT_RESTORED")


def a171_resource_observer_receipt(data, spec, plan, cap):
    global A171_OBSERVATION, A171_RAW_FRAMES
    A171_RAW_FRAMES = []
    A171_OBSERVATION = a171_new_observation()
    consumer = spec["expected"]["consumer"]
    A171_OBSERVATION["resource_precondition"] = {
        "domain": "original_observer_before_inner_leaf", "passed": False,
        "entered_inner_leaf": False, "joined_delegated_producer": False,
        "prepared_delegated_producer": False}
    cleanup = []
    before = None
    primary = None
    original_error = None
    passed = False
    outcome = None
    actual_cause = None
    try:
        before = observation()
    except BaseException as exc:
        cleanup.append(x.cause(exc))
    try:
        if consumer == "G1.resources":
            resources = a171_called(consumer, "G1.resources", x.m.resources)
        elif consumer == "acquisition_resources":
            resources = a171_called(consumer, "acquisition_resources", x.acquisition_resources)
        else:
            raise A171CodeGap("ORIGINAL_RESOURCE_CONSUMER:" + consumer)
        A171_OBSERVATION["resource_precondition"]["passed"] = True
        A171_OBSERVATION["resource_precondition"]["actual_consumer"] = consumer
        if type(resources) is dict:
            keep = ("aggregate_envelope", "outer_budget", "inner_budget", "outer_as",
                    "coordinator_as", "worker_as_each")
            A171_OBSERVATION["facts"]["resource_envelope"] = {key: resources[key] for key in keep if key in resources}
        fault = A171_OBSERVATION["first_fault"]
        actual_cause = None if fault is None else fault["cause"]
        outcome = "RETURNED" if actual_cause is None else "REFUSED"
        if A171_OBSERVATION["state"] is None:
            A171_OBSERVATION["state"] = "CONSUMER_ACCEPTED_NO_ADMISSION" if actual_cause is None else "REFUSED"
        a171_validate_observation(A171_OBSERVATION, spec["expected"], c.need, data["id"])
        passed = True
    except BaseException as exc:
        primary = x.cause(exc)
        original_error = exc
        passed = False
        fault = A171_OBSERVATION["first_fault"]
        if fault is not None:
            actual_cause = fault["cause"]
            outcome = "REFUSED"
    producer = {"schema": "friday.a158.performing-producer-result.v1", "id": data["id"],
                "position": data["position"], "generation": 1, "producer": spec["producer"],
                "delegated_fork": False, "operation_outcome": outcome, "actual_cause": actual_cause,
                "operation_observation": A171_OBSERVATION, "body_complete": False,
                "acceptance_complete": False, "SourceReady": False, "GO": False}
    return {"schema": "friday.a158.whole216-controller-result.v1", "id": data["id"],
            "position": data["position"], "generation": 1, "producer_called": False, "child_created": False,
            "producer": producer, "producer_streams": [], "actual_auxiliary_cleanup": cleanup,
            "primary_error": primary, "original_error": original_error, "cleanup_errors": cleanup,
            "session_before": before, "session_after": None, "native_child": None, "native_evidence": None,
            "operation_outcome": outcome, "actual_cause": actual_cause, "passed": passed,
            "disposition": "SOURCE_CAUSAL_ROUTE_OBSERVED_NO_WHOLE_ADMISSION" if passed else "REFUSED_SCOPED_RECEIPT",
            "terminal_completion": passed, "body_complete": False, "acceptance_complete": False,
            "SourceReady": False, "GO": False, "whole216_credit": False, "F10_waiver": False,
            "whole_assignment_RAM_and_implicit_IO": "UNKNOWN_NOT_ZERO_NOT_PROVEN"}

def controls_whole216(raw,plan):
    global WHOLE216_CUSTODY
    data,spec=whole216_input(raw,plan);cap=c.ADMISSION;lib=c.NATIVE
    c.need(cap is not None and cap["mode_id"]==1 and lib is not None,"A158_SAME_ADMITTED_NATIVE_MODE1")
    if not spec["active"]:
        return {"schema":"friday.a158.whole216-controller-result.v1","id":data["id"],"position":data["position"],
            "disposition":"OPEN_SOURCE_CODE_GAP","mandatory_cause":"NO_ACTUAL_PERFORMING_SOURCE_PRODUCER",
            "producer_called":False,"child_created":False,"terminal_completion":False,
            "body_complete":False,"acceptance_complete":False,"SourceReady":False,"GO":False}
    if spec["expected"]["owned_children"]:
        receipt=a171_coordinator_receipt(data,spec,plan,cap)
        WHOLE216_CUSTODY=receipt
        return receipt
    if spec["expected"]["consumer"] in ("G1.resources","acquisition_resources"):
        receipt=a171_resource_observer_receipt(data,spec,plan,cap)
        WHOLE216_CUSTODY=receipt
        return receipt
    c.need(lib.fr_controller_prepare(data["position"])==0,"A158_ROOT_SELECTED_PREPARE_REQUEST")
    # All records and guards precede first real acquisition. Neither a supplied
    # PID nor a caller status can create this native-owned intention.
    intention=c.Child();intention.pidfd=-1;before=observation()
    entries=[{"fd":None,"attempted":False,"closed":False,"close_error":None,"original_error":None} for _ in range(4)]
    pairs=[];buffers=[bytearray(),bytearray()];active=[False,False];seen=[0,0];eof=[False,False]
    overflow=[False,False];read_errors=[None,None];retention_errors=[None,None];identities=[None,None]
    pid=None;primary=None;original_error=None;cleanup=[];native_wait=None;release=None;producer=None;trace=None;final=None
    def close(slot):
        entry=entries[slot]
        if entry["fd"] is None:return True
        if entry["attempted"]:return entry["closed"]
        entry["attempted"]=True
        try:os.close(entry["fd"])
        except BaseException as exc:entry["close_error"]=x.cause(exc);entry["original_error"]=exc;return False
        entry["closed"]=True;return True
    def drain():
        for index in range(2):
            if not active[index]:continue
            fd=entries[2*index]["fd"]
            try:part=os.read(fd,65536)
            except BlockingIOError:continue
            except BaseException as exc:
                read_errors[index]=x.cause(exc);active[index]=False;close(2*index);continue
            if not part:eof[index]=True;active[index]=False;close(2*index);continue
            seen[index]+=len(part)
            try:buffers[index].extend(part[:max(0,1048576-len(buffers[index]))])
            except BaseException as exc:retention_errors[index]=x.cause(exc)
            if seen[index]>1048576:overflow[index]=True
    try:
        for index in range(2):
            pair=os.pipe2(os.O_CLOEXEC);entries[2*index]["fd"]=pair[0];entries[2*index+1]["fd"]=pair[1]
            pairs.append(pair);identities[index]=[str(v) for v in x.identity(os.fstat(pair[0]))]
            os.set_blocking(pair[0],False);active[index]=True
        pid=lib.fr_own_fork(123,0,cap["work_ns"],ctypes.byref(intention))
        if pid==0:
            try:
                for read,_ in pairs:os.close(read)
                os.dup2(pairs[0][1],1);os.dup2(pairs[1][1],2)
                for _,write in pairs:os.close(write)
                value=whole216_producer(data,spec,plan)
                message=(json.dumps(value,separators=(",",":"))+"\n").encode("ascii")
                c.need(len(message)<=32768,"A158_NORMAL_PRODUCER_EMISSION_BOUND")
                frames=b"".join((len(blob).to_bytes(4,"big")+blob) for blob in A171_RAW_FRAMES)
                packet=message+frames
                c.need(len(packet)<=1048576,"A171_ORIGINAL_STREAM_BOUND_FULL_RAW")
                os.set_blocking(1,False);view=memoryview(packet)
                while view and time.monotonic_ns()<cap["work_ns"]:
                    try:n=os.write(1,view)
                    except BlockingIOError:select.select([], [1], [], .002);continue
                    c.need(n>0,"A158_NORMAL_PRODUCER_WRITE");view=view[n:]
                c.need(not view,"A158_PRODUCER_ORIGINAL_END");os._exit(0)
            except BaseException:os._exit(125)
        # Native storage wrote the actual clone/PID/pidfd before its checks;
        # copy is observed here before any endpoint close or caller setup.
        c.need(intention.pid>0,"A158_ACTUAL_CONTROLLER_CLONE_INTENTION")
        for slot in (1,3):c.need(close(slot),"A158_PARENT_WRITER_CLOSE")
        c.need(pid>0 and intention.pid==pid and intention.state==2,"A158_ACTUAL_REGISTER_ACK_BEFORE_PRODUCER")
        while any(active) or native_wait!=1:
            c.need(time.monotonic_ns()<cap["hard_ns"]-2*10**9,"A158_ORIGINAL_BOUNDED_CONTROLLER_END")
            drain()
            if native_wait!=1:
                native_wait=lib.fr_own_wait(123,ctypes.byref(intention),1)
                c.need(native_wait>=0,"A158_PRIVATE_WAIT_REAP_ACK:%d"%native_wait)
            c.need(not any(overflow) and not any(read_errors) and not any(retention_errors),"A158_ORIGINAL_BOTH_STREAM_CAP_READ_RETENTION")
            select.select([pairs[i][0] for i in range(2) if active[i]],[],[],.002)
    except BaseException as exc:primary=x.cause(exc);original_error=exc
    finally:
        # Both writer aliases close even on parent setup failure. Same native
        # record/generation supplies finite disposal, never a numeric adoption.
        close(1);close(3)
        if intention.pid>0:
            try:
                state=public_observe(intention)
                if not state["cleanup_reaped"]:
                    stopped=lib.fr_own_stop(123,ctypes.byref(intention),min(cap["hard_ns"]-10**9,time.monotonic_ns()+10**9))
                    if stopped<0:cleanup.append("NATIVE_STOP:%d"%stopped)
            except BaseException as exc:cleanup.append(x.cause(exc));original_error=original_error or exc
            # Drain remains reachable even if the preceding owned stop or
            # observation refused; actual byte custody does not depend on ACK.
            end=min(cap["hard_ns"]-10**9,time.monotonic_ns()+10**9)
            while any(active) and time.monotonic_ns()<end:
                try:drain();select.select([pairs[i][0] for i in range(2) if active[i]],[],[],.002)
                except BaseException as exc:cleanup.append(x.cause(exc));original_error=original_error or exc;break
            try:
                state=public_observe(intention)
                if state["cleanup_reaped"]:release=lib.fr_own_release(ctypes.byref(intention))
                final=public_observe(intention);trace=public_evidence(intention)
                if not final["cleanup_reaped"] or not final["handle_closed"] or release is not None and release<0:
                    cleanup.append("STOP_UNCONFIRMED")
            except BaseException as exc:cleanup.append(x.cause(exc));original_error=original_error or exc
        for slot in range(4):
            if not close(slot):cleanup.append("FD_CLOSE_UNCONFIRMED")
    streams=[]
    for index in range(2):
        # These immutable original objects exist before frame/semantic checks.
        try:raw_bytes=bytes(buffers[index])
        except BaseException as exc:
            retention_errors[index]=x.cause(exc);primary=primary or x.cause(exc);original_error=original_error or exc;raw_bytes=None
        complete=eof[index] and not overflow[index] and not read_errors[index] and not retention_errors[index] and \
            entries[2*index]["closed"] and raw_bytes is not None and seen[index]==len(raw_bytes)
        raw_hex=None;raw_sha=None
        if raw_bytes is not None:
            try:raw_hex=raw_bytes.hex();raw_sha=hashlib.sha256(raw_bytes).hexdigest()
            except BaseException as exc:
                retention_errors[index]=x.cause(exc);primary=primary or x.cause(exc)
                original_error=original_error or exc;complete=False
        streams.append({"stream":index,"cap":1048576,"raw":raw_bytes,
            "retained_on_freeze_error":buffers[index] if raw_bytes is None else None,
            "raw_hex":raw_hex,
            "raw_size":len(raw_bytes) if raw_bytes is not None else None,"retained_size":len(buffers[index]),
            "total_seen":seen[index],"eof":eof[index],"overflow":overflow[index],
            "read_error":read_errors[index],"retention_error":retention_errors[index],
            "close_error":entries[2*index]["close_error"],"sha256":raw_sha,
            "pipe_identity9":identities[index],"original_complete":complete,"hash_only":False})
    passed=False;outcome=None;actual_cause=None
    if primary is None and not cleanup:
        try:
            c.need(all(v["original_complete"] for v in streams) and not buffers[1],"A158_FULL_ORIGINAL_PRODUCER_CUSTODY")
            raw_bytes=bytes(buffers[0]);newline=raw_bytes.find(b"\n")
            c.need(newline>=0,"A158_PERFORMING_PRODUCER_FRAME")
            producer=x.m.inert_json(raw_bytes[:newline]+b"\n",32768)
            c.need(producer.get("schema")=="friday.a158.performing-producer-result.v1" and
                producer.get("id")==data["id"] and producer.get("position")==data["position"] and
                producer.get("generation")==1 and producer.get("producer")==spec["producer"],"A158_ACTUAL_PER_ID_PERFORMING_OUTPUT")
            a171_restore_raw_frames(producer, raw_bytes[newline+1:])
            outcome=producer["operation_outcome"];actual_cause=producer["actual_cause"]
            c.need(actual_cause==spec["expected"]["cause"] and outcome==("RETURNED" if actual_cause is None else "REFUSED"),
                "A158_EXPECTED_ACTUAL_CONSUMER_CAUSE")
            c.need(final is not None and final["status_known"]==1 and final["status"]==0 and
                trace["wait4_ns"]>0 and trace["reap_ack_ns"]>=trace["wait4_ns"] and final["handle_closed"]==1,
                "A158_SAME_PRIVATE_KERNEL_WAIT_ROOT_ACK_CLOSE")
            a171_validate_observation(producer["operation_observation"],spec["expected"],c.need,data["id"])
            passed=True
        except BaseException as exc:primary=x.cause(exc);original_error=exc
    receipt={"schema":"friday.a158.whole216-controller-result.v1","id":data["id"],"position":data["position"],
        "generation":1,"producer_called":intention.pid>0,"child_created":intention.pid>0,"producer":producer,
        "producer_streams":streams if intention.pid>0 else None,
        "actual_auxiliary_cleanup":[entry for entry in entries if entry["fd"] is not None],
        "primary_error":primary,"original_error":original_error,"cleanup_errors":cleanup,
        "session_before":before,"session_after":observation(),"native_child":final,"native_evidence":trace,
        "operation_outcome":outcome,"actual_cause":actual_cause,"passed":passed,
        "disposition":"SOURCE_CAUSAL_ROUTE_OBSERVED_NO_WHOLE_ADMISSION" if passed else "STOP_UNCONFIRMED" if cleanup else "REFUSED_SCOPED_RECEIPT",
        "terminal_completion":passed,"body_complete":False,"acceptance_complete":False,"SourceReady":False,"GO":False,
        "whole216_credit":False,"F10_waiver":False,"whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN"}
    WHOLE216_CUSTODY=receipt
    return receipt

def whole216_wire(receipt):
    # The original returned receipt retains actual immutable bytes/error and
    # freeze-failure mutable buffers. Only a separate bounded transport view
    # omits Python objects; full raw_hex remains bound to the retained bytes.
    wire={key:value for key,value in receipt.items() if key!="original_error"}
    wire["actual_auxiliary_cleanup"]=[{key:value for key,value in entry.items() if key!="original_error"}
        for entry in receipt.get("actual_auxiliary_cleanup",[])]
    if receipt.get("producer_streams") is not None:
        wire["producer_streams"]=[{key:value for key,value in stream.items()
            if key not in ("raw","retained_on_freeze_error")} for stream in receipt["producer_streams"]]
    wire["original_error_retained_in_same_process"]=receipt.get("original_error") is not None
    wire["raw_custody_domain"]="same_coordinator_receipt_then_explicit_raw_hex_transport_not_crash_durability"
    return wire

# Data-only bothside validation. Exact original table/order/cause is separate.
def a171_validate_input(data,meta,need):
    args=data["arguments"];consumer=meta["consumer"];case=data["id"]
    if data["producer"]!="a171_actual":return
    schemas={"sealed_bytes":{"parent","relative","sha256","cap"},
        "require_absent_target":{"parent","relative"},"RetainedTree.check":{"parent","inventory"},
        "BoundedOutput/G1.Output.check/create":{"parent","relative","raw_hex"},
        "R4.guarded_digest":{"parent","relative","cap","sha256"},
        "ca_context/R4.guarded_digest":{"parent","relative","cap","sha256"},
        "ca_context/SSLContext.load_verify_locations":{"parent","relative","cap","sha256"},
        "G1.resources":set(),"acquisition_resources":set(),
        "Supervisor.runtime_preflight":{"parent","manifest_sha256"},
        "Supervisor.cgroup_values":{"parent"},"Supervisor.HeldSource.bytes":{"parent","relative","sha256"},
        "Supervisor.seal":{"parent","relative","sha256"},
        "Supervisor.NativeLaunchAdapter":{"parent","relative","sha256"},
        "terminal_bytes":{"value"},"Run.guard":set()}
    wanted=schemas.get(consumer,set())
    if case=="held_path_replaced_exact_bytes":wanted=wanted|{"mutation_hex"}
    if consumer=="RetainedTree.check" and case!="retained_positive":wanted=wanted|{"mutation_hex"}
    if consumer=="emit_terminal" and case in ("terminal_output_path","terminal_sink_blocked"):wanted={"value"}
    if case=="outer_sink_blocked":wanted={"value"}
    if meta["owned_children"]:wanted=set()
    need(type(args) is dict and set(args)==wanted,"A171_BOTHSIDE_EXACT_TYPED_OPERANDS")
    if "parent" in args:
        need(type(args["parent"]) is str and len(args["parent"])<=256 and
            args["parent"].startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
            os.path.dirname(args["parent"])=="/var/tmp","A171_ORIGINAL_OWNED_INPUT_SCOPE")
    if "relative" in args:
        need(type(args["relative"]) is str and len(args["relative"])<=256 and
            all(part not in ("",".","..") for part in args["relative"].split("/")),"A171_OWNED_PATH_OPERAND")
    for key in ("sha256","manifest_sha256"):
        if key in args:need((case=="outer_unknown_pin" and key=="sha256" and args[key] is None) or
            (type(args[key]) is str and len(args[key])==64 and
            all(ch in "0123456789abcdef" for ch in args[key])),"A171_TYPED_SHA_OPERAND")
    if "cap" in args:need(type(args["cap"]) is int and 0<=args["cap"]<=2147483648,"A171_EXACT_INTEGER_CAP")
    for key in ("raw_hex","mutation_hex"):
        if key in args:need(type(args[key]) is str and len(args[key])<=524288 and
            len(args[key])%2==0 and all(ch in "0123456789abcdef" for ch in args[key]),"A171_OWNED_INERT_BYTES")
    if "inventory" in args:need(type(args["inventory"]) is dict and
        args["inventory"].get("root")==args["parent"]+"/held","A171_ORIGINAL_RETAINED_INPUT_DOMAIN")

# A171 inert Source text. No fixture constructor, network, exec or owner reset.
# The external original caller selects operands in its existing sealed fd119.
# A failure to implement an original domain remains CODE_GAP, never an oracle pass.
A171_OBSERVATION=None
A171_RAW_FRAMES=[]

class A171CodeGap(Exception):
    pass

def a171_cause(exc):
    if isinstance(exc,(c.Refused,s.Refused,x.AdmissionRefused,x.m.Refused)):
        return str(exc)[:512]
    return x.cause(exc)

def a171_identity(st):
    return [str(v) for v in (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,
        st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)]

def a171_new_observation():
    return {"schema":"friday.a171.actual-operation-observation.v1","state":None,
        "stages":{},"started_routes":0,"hashes":[],"target_children":[],
        "consumer_calls":[],"performer_entries":0,"phase_order":[],"facts":{},
        "first_fault":None,"resource_precondition":None,"unavailable_reason":None,
        "operation_raw":[],"descriptor_records":[],"runtime":"NOT_RUN_BY_AUTHOR"}

def a171_called(consumer,stage,fn,*args,**kwargs):
    observation=A171_OBSERVATION
    if observation is not None:
        observation["stages"][stage]=observation["stages"].get(stage,0)+1
        observation["phase_order"].append({"phase":"consumer_enter","consumer":consumer,
            "stage":stage,"at_ns":str(time.monotonic_ns())})
        observation["consumer_calls"].append({"consumer":consumer,"stage":stage,
            "returned":False,"error":None})
        call=observation["consumer_calls"][-1]
    try:
        value=fn(*args,**kwargs)
    except BaseException as exc:
        if observation is not None:
            # Preserve the actual SSL parser reason in the original local CA domain.
            reason=exc.reason if isinstance(exc,x.ssl.SSLError) and stage=="ca_preflight" else a171_cause(exc)
            call["error"]=reason
            if observation["first_fault"] is None:
                observation["first_fault"]={"cause":reason,"stage":stage,
                    "consumer":consumer,"at_ns":str(time.monotonic_ns())}
            observation["phase_order"].append({"phase":"consumer_error","consumer":consumer,
                "stage":stage,"cause":reason,"at_ns":str(time.monotonic_ns())})
        raise
    if observation is not None:
        call["returned"]=True
        observation["phase_order"].append({"phase":"consumer_return","consumer":consumer,
            "stage":stage,"at_ns":str(time.monotonic_ns())})
    return value

def a171_run():
    # Same original capsule ends. make_run's fresh defaults are replaced before use.
    run=x.make_run(clock=time.monotonic_ns)
    run.started=c.ADMISSION["start_ns"]
    run.work=c.ADMISSION["work_ns"]
    run.hard=c.ADMISSION["hard_ns"]
    return run

def a171_keys(args,keys):
    c.need(type(args) is dict and set(args)==set(keys),"A171_EXACT_OPERATION_OPERANDS")

def a171_parent(value):
    c.need(type(value) is str and value.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
        os.path.dirname(value)=="/var/tmp" and len(value)<=256,"A171_ORIGINAL_OWNED_DOMAIN")
    fd=x.open_nf(value)
    try:
        st=os.fstat(fd)
        c.need(x.stat.S_ISDIR(st.st_mode) and st.st_uid==st.st_gid==os.getuid()==1000 and
            x.stat.S_IMODE(st.st_mode)==0o700,"A171_OWNED_PARENT_CUSTODY")
        A171_OBSERVATION["facts"]["parent_identity9"]=a171_identity(st)
    finally:os.close(fd)
    return value

def a171_path(parent,relative):
    c.need(type(relative) is str and len(relative)<=256 and
        all(v not in ("",".","..") for v in relative.split("/")),"A171_OWNED_RELATIVE_PATH")
    return parent+"/"+relative

def a171_fd_record(custody):
    record={"fd":None,"owner":os.getpid(),"identity9":None,"close_attempted":False,
        "closed":False,"close_error":None,"original_error":None}
    custody.append(record)
    return record

def a171_close(record):
    if record["fd"] is None:return
    if record["close_attempted"]:return
    record["close_attempted"]=True
    try:os.close(record["fd"])
    except BaseException as exc:
        record["close_error"]=x.cause(exc);record["original_error"]=exc
    else:record["closed"]=True

def a171_visible_dependency_fds(obj, seen, found):
    if obj is None or id(obj) in seen: return
    seen.add(id(obj))
    for attr in ("fds","dirs","holds"):
        mapping=getattr(obj, attr, None)
        if type(mapping) is dict:
            for fd in mapping.values():
                if type(fd) is int and fd>=0: found.append(fd)
    inner=getattr(obj, "inner", None)
    if inner is not None: a171_visible_dependency_fds(inner, seen, found)

def a171_journal_dependencies(custody, obj):
    existing={record["fd"] for record in custody if type(record.get("fd")) is int}
    found=[];a171_visible_dependency_fds(obj, set(), found);records=[]
    for fd in found:
        if fd in existing: continue
        record=a171_fd_record(custody);record["fd"]=fd;record["dependency"]=True
        try: record["identity9"]=a171_identity(os.fstat(fd))
        except OSError as exc:
            record["close_error"]=x.cause(exc);record["original_error"]=exc
        records.append(record);existing.add(fd)
    return records

def a171_confirm_dependency_close(record):
    if record.get("fd") is None or record.get("dependency") is not True: return
    if record["close_attempted"]: return
    recorded=record.get("identity9")
    try: now=a171_identity(os.fstat(record["fd"]))
    except OSError as exc:
        if exc.errno==errno.EBADF:
            record["close_attempted"]=True;record["closed"]=True;record["close_error"]=None
            record["dependency_close"]="CONFIRMED_EBADF";return
        record["close_attempted"]=True;record["closed"]=False
        record["close_error"]=x.cause(exc);record["original_error"]=exc
        A171_OBSERVATION["facts"]["sticky_uncertainty"]=True
        A171_OBSERVATION["state"]="STOP_UNCONFIRMED";return
    if now==recorded:
        a171_close(record)
        if record["closed"] is not True:
            A171_OBSERVATION["facts"]["sticky_uncertainty"]=True
            A171_OBSERVATION["state"]="STOP_UNCONFIRMED"
        return
    record["close_error"]="A171_DEPENDENCY_IDENTITY_CHANGED"
    A171_OBSERVATION["facts"]["sticky_uncertainty"]=True
    A171_OBSERVATION["state"]="STOP_UNCONFIRMED"

def a171_borrow_source(role,custody):
    c.need(type(role) is str and role in s.SOURCE_ROLES,"A171_FIXED_EXISTING_SOURCE_ROLE")
    record=a171_fd_record(custody)
    record["fd"]=os.dup(s.SOURCE_ROLES[role][0])
    record["identity9"]=a171_identity(os.fstat(record["fd"]))
    return record

def a171_result_bytes(raw,label):
    global A171_RAW_FRAMES
    c.need(type(raw) is bytes and len(raw)<=262144,"A171_OPERATION_RAW_CAP")
    digest=hashlib.sha256(raw).hexdigest()
    if len(raw)*2>2048:
        A171_RAW_FRAMES.append(raw)
        A171_OBSERVATION["operation_raw"].append({"label":label,"transport":"stdout_frame","raw_hex":None,
            "bytes":len(raw),"sha256":digest,"hash_only":False})
    else:
        A171_OBSERVATION["operation_raw"].append({"label":label,"raw_hex":raw.hex(),
            "bytes":len(raw),"sha256":digest,"hash_only":False})

def a171_create_owned(path,raw,custody):
    record=a171_fd_record(custody)
    try:
        c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A171_ORIGINAL_MUTATION_END")
        record["fd"]=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        record["identity9"]=a171_identity(os.fstat(record["fd"]))
        view=memoryview(raw)
        while view:
            c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A171_ORIGINAL_MUTATION_END")
            count=os.write(record["fd"],view);c.need(count>0,"A171_ACTUAL_OWNED_WRITE")
            view=view[count:]
        os.fsync(record["fd"])
        return a171_identity(os.fstat(record["fd"]))
    finally:a171_close(record)

def a171_retained_control(case,held,parent,args,custody):
    if case=="retained_positive":return None
    key=next(iter(held.fds));path=held.root+"/"+key;original=held.fds[key]
    c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A171_ORIGINAL_MUTATION_END")
    before=a171_identity(os.fstat(original));mutation={"case":case,"before_identity9":before}
    raw=whole216_hex(args["mutation_hex"],65536)
    c.need(bool(raw),"A171_ROOT_SELECTED_ORIGINAL_INERT_MUTATION_BYTES")
    if case in ("retained_content","late_input"):
        record=a171_fd_record(custody)
        try:
            record["fd"]=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
            record["identity9"]=a171_identity(os.fstat(record["fd"]))
            c.need(record["identity9"]==before,"A171_SAME_ACTUAL_OWNED_RETAINED_FILE")
            count=os.pwrite(record["fd"],raw,0)
            c.need(count==len(raw),"A171_ACTUAL_RETAINED_MUTATION_WRITE")
            os.fsync(record["fd"])
        finally:a171_close(record)
        mutation["after_identity9"]=a171_identity(os.fstat(original))
    elif case=="retained_path":
        new_path=path+".a171-owned-new"
        mutation["replacement_identity9"]=a171_create_owned(new_path,raw,custody)
        os.replace(new_path,path)
        mutation["named_after_identity9"]=a171_identity(os.stat(path,follow_symlinks=False))
    elif case=="retained_fd":
        record=a171_fd_record(custody);record["fd"]=original;record["identity9"]=before
        a171_close(record);c.need(record["closed"],"A171_RETAINED_NEGATIVE_CLOSE_UNCONFIRMED")
        held.fds[key]=-1
        mutation["actual_closed_fd"]=original
        return key,mutation
    elif case=="retained_directory":
        saved=parent+"/a171-owned-saved-directory"
        c.need(not os.path.lexists(saved),"A171_OWNED_SAVE_COLLISION")
        directory=held.root+"/d00"
        os.rename(directory,saved);os.mkdir(directory,0o700)
        mutation["directory_named_after_identity9"]=a171_identity(os.stat(directory,follow_symlinks=False))
        mutation["directory_held_identity9"]=a171_identity(os.fstat(held.dirs["d00"]))
    elif case=="retained_membership":
        mutation["created_identity9"]=a171_create_owned(held.root+"/a171-owned-extra",raw,custody)
    else:raise A171CodeGap("ORIGINAL_RETAINED_CONTROL_PHASE_NOT_CONNECTED:"+case)
    return None,mutation

def a171_output_control(case,out,parent,root,raw,custody):
    if case=="output_positive":return False
    path=root+"/bill.json";before=a171_identity(os.stat(path,follow_symlinks=False))
    mutation={"case":case,"before_identity9":before}
    c.need(time.monotonic_ns()<c.ADMISSION["work_ns"],"A171_ORIGINAL_MUTATION_END")
    if case=="output_mode":os.chmod(path,0o644)
    elif case=="output_hardlink":os.link(path,parent+"/a171-owned-link",follow_symlinks=False)
    elif case=="output_symlink":
        os.unlink(path);os.symlink(parent+"/a171-owned-absent",path)
    elif case=="output_membership":
        mutation["created_identity9"]=a171_create_owned(root+"/a171-owned-extra",raw,custody)
    elif case=="output_root":
        saved=parent+"/a171-owned-saved-output"
        c.need(not os.path.lexists(saved),"A171_OWNED_SAVE_COLLISION")
        os.rename(root,saved);os.mkdir(root,0o700)
    elif case=="output_directory":
        saved=parent+"/a171-owned-saved-output-directory"
        c.need(not os.path.lexists(saved),"A171_OWNED_SAVE_COLLISION")
        os.rename(root+"/archives/playwright",saved);os.mkdir(root+"/archives/playwright",0o700)
    elif case=="output_disk":
        target=x.LIMITS["disk_bytes_max"]+1
        soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
        mutation["declared_sparse_target"]=target
        mutation["unchanged_fsize_soft"]=soft
        mutation["allocation_domain"]="aggregate_checker_observes_size"
        mutation["performer_allocation"]="NOT_ATTEMPTED_ABOVE_UNCHANGED_FSIZE"
        if soft==resource.RLIM_INFINITY or soft>=target:
            record=a171_fd_record(custody)
            try:
                record["fd"]=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
                record["identity9"]=a171_identity(os.fstat(record["fd"]))
                c.need(record["identity9"]==before,"A171_SAME_ACTUAL_OWNED_OUTPUT_FILE")
                os.ftruncate(record["fd"],target)
            finally:a171_close(record)
    elif case=="output_collision":
        A171_OBSERVATION["facts"]["controlled_mutation"]=mutation
        return True
    else:raise A171CodeGap("ORIGINAL_OUTPUT_CONTROL_PHASE_NOT_CONNECTED:"+case)
    if case!="output_root":
        mutation["named_after_identity9"]=a171_identity(os.stat(path,follow_symlinks=False))
    else:
        mutation["named_after_root_identity9"]=a171_identity(os.stat(root,follow_symlinks=False))
    A171_OBSERVATION["facts"]["controlled_mutation"]=mutation
    return False

def a171_merge_run(run):
    observation=A171_OBSERVATION
    prior_sticky=observation["facts"].get("sticky_uncertainty") is True
    observation["stages"].update(run.stages)
    observation["started_routes"]=len(run.started_paths)
    observation["hashes"]=list(run.hashes)
    observation["target_children"]=[{"pid":child["pid"],"lifecycle":child["lifecycle"],
        "connected":child["connected"]} for child in run.children]
    observation["facts"]["run_reason"]=run.reason
    observation["facts"]["sticky_uncertainty"]=True if prior_sticky or run.uncertain else False
    observation["facts"]["run_errors"]=list(run.errors)
    if run.reason is not None and observation["first_fault"] is None:
        calls=observation["consumer_calls"]
        observation["first_fault"]={"cause":run.reason,
            "stage":calls[-1]["stage"] if calls else None,
            "consumer":calls[-1]["consumer"] if calls else None,"at_ns":str(time.monotonic_ns())}

def a171_perform(data,spec,plan,custody):
    args=data["arguments"];consumer=spec["expected"]["consumer"];case=data["id"]
    observation=A171_OBSERVATION;observation["performer_entries"]+=1
    # Target-child domains are not redirected into an inherited producer session.
    # Their caller is now connected, but the coordinator integration is an exact gap.
    if spec["expected"]["owned_children"]:
        raise A171CodeGap("ORIGINAL_COORDINATOR_TARGET_CHILD_ROUTE_REQUIRED:"+case)
    run=a171_run()
    try:
        if consumer=="sealed_bytes":
            a171_keys(args,("parent","relative","sha256","cap"))
            parent=a171_parent(args["parent"]);path=a171_path(parent,args["relative"])
            c.need(type(args["cap"]) is int and 0<=args["cap"]<=1048576,"A171_ORIGINAL_PIN_CAP")
            value=a171_called("sealed_bytes","sealed_bytes",x.sealed_bytes,path,args["sha256"],args["cap"])
            a171_result_bytes(value,"sealed_bytes")
            return {"actual_size":len(value),"actual_sha256":hashlib.sha256(value).hexdigest()}
        if consumer=="require_absent_target":
            a171_keys(args,("parent","relative"))
            path=a171_path(a171_parent(args["parent"]),args["relative"])
            a171_called(consumer,"require_absent_target",x.require_absent_target,path)
            return {"target_path":path,"actual_absent":not os.path.lexists(path)}
        if consumer=="RetainedTree.check":
            if case=="late_input":
                observation["unavailable_reason"]="ORIGINAL_COORDINATOR_ABORT_STATE_ROUTE_REQUIRED:late_input"
            a171_keys(args,("parent","inventory") if case=="retained_positive" else
                ("parent","inventory","mutation_hex"))
            parent=a171_parent(args["parent"])
            c.need(type(args["inventory"]) is dict and args["inventory"].get("root")==parent+"/held",
                "A171_ORIGINAL316_13_OWNED_RETAINED_DOMAIN")
            held=None;closed_key=None;dependency_records=[]
            try:
                held=a171_called("RetainedTree.__init__","retained_inventory_admission",
                    x.RetainedTree,parent+"/held",args["inventory"],run)
                observation["facts"]["admitted_files"]=len(held.fds)
                observation["facts"]["admitted_directories"]=len(held.dirs)
                dependency_records=a171_journal_dependencies(custody, held)
                controlled=a171_retained_control(case,held,parent,args,custody)
                if controlled is not None:
                    closed_key,mutation=controlled
                    observation["facts"]["controlled_mutation"]=mutation
                a171_called(consumer,"retained_partial_custody",held.check)
                identities={key:a171_identity(os.fstat(fd)) for key,fd in held.fds.items()}
                return {"files":len(held.fds),"directories":len(held.dirs),
                    "actual_file_identity_set_sha256":hashlib.sha256(
                        json.dumps(identities,sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest()}
            finally:
                try:
                    if held is not None:
                        if closed_key is not None:held.fds.pop(closed_key,None)
                        held.close()
                finally:
                    for record in dependency_records: a171_confirm_dependency_close(record)
        if consumer=="BoundedOutput/G1.Output.check/create":
            a171_keys(args,("parent","relative","raw_hex"))
            root=a171_path(a171_parent(args["parent"]),args["relative"])
            raw=whole216_hex(args["raw_hex"],262144);out=None;record=a171_fd_record(custody);dependency_records=[]
            try:
                out=a171_called("BoundedOutput.__init__","output_custody",x.BoundedOutput,
                    root,plan["retained_paths"],plan["retained_dirs"],run)
                dependency_records=a171_journal_dependencies(custody, out)
                record["fd"]=a171_called("BoundedOutput.create","output_custody",out.create,"bill.json")
                record["identity9"]=a171_identity(os.fstat(record["fd"]))
                a171_called("R4.guarded_write","output_payload_write",x.r.guarded_write,
                    record["fd"],raw,run,"output_payload_write")
                os.fsync(record["fd"]);a171_close(record)
                collide=a171_output_control(case,out,args["parent"],root,raw,custody)
                size=a171_called(consumer,"output_custody",out.create,"bill.json") if collide else \
                    a171_called(consumer,"output_custody",out.check)
                a171_result_bytes(raw,"actual_written_output_payload")
                return {"actual_created":sorted(out.inner.created),"actual_disk_bytes":size}
            finally:
                try:
                    a171_close(record)
                    if out is not None:out.close()
                finally:
                    for record in dependency_records: a171_confirm_dependency_close(record)
        if consumer in ("R4.guarded_digest","ca_context/R4.guarded_digest",
                "ca_context/SSLContext.load_verify_locations"):
            a171_keys(args,("parent","relative","cap","sha256"))
            path=a171_path(a171_parent(args["parent"]),args["relative"])
            c.need(type(args["cap"]) is int and 0<=args["cap"]<=2147483648,"A171_ORIGINAL_DIGEST_CAP")
            if consumer.startswith("ca_context/"):
                context=a171_called(consumer,"ca_preflight",x.ca_context,run,path,args["sha256"])
                return {"minimum_version":int(context.minimum_version),"verify_mode":int(context.verify_mode),
                    "check_hostname":context.check_hostname,"network_used":False}
            record=a171_fd_record(custody)
            try:
                record["fd"]=x.open_nf(path);record["identity9"]=a171_identity(os.fstat(record["fd"]))
                stage="hash_network" if case=="pinned_body_file_cap" else "guarded_hash"
                count,digest,_=a171_called(consumer,stage,x.r.guarded_digest,
                    record["fd"],args["cap"],run,stage,args["sha256"],terminal=True)
                return {"actual_fd_bytes":count,"actual_fd_sha256":digest}
            finally:a171_close(record)
        if consumer=="R4.guarded_write":
            # Original timeout testing needs a controlled phase at the original end.
            # Do not open a writer merely to manufacture that deadline.
            raise A171CodeGap("ORIGINAL_GUARDED_WRITE_DEADLINE_PHASE_NOT_CONNECTED")
        if consumer=="G1.resources":
            a171_keys(args,())
            # Actual immutable helper, actual proc/mount/leaf/ancestors/disk/host order.
            # Its 256MiB demand is intentionally not replaced by the 192MiB leaf.
            return a171_called(consumer,"G1.resources",x.m.resources)
        if consumer=="acquisition_resources":
            a171_keys(args,())
            return a171_called(consumer,"acquisition_resources",x.acquisition_resources)
        if consumer=="Supervisor.runtime_preflight":
            a171_keys(args,("parent","manifest_sha256"))
            parent=a171_parent(args["parent"])
            view=s.RuntimeView(parent+"/runtime",fixture=True)
            closure=None
            try:
                # The original approved owned filesystem domain, full stdlib/ELF/cache.
                # No zero-filled substitute, false library flags, or partial ELF path.
                actual_manifest_path=parent+"/runtime/manifest.json" if case=="outer_runtime_path" else s.RUNTIME
                closure=a171_called(consumer,"Supervisor.runtime_preflight",s.runtime_preflight,
                    actual_manifest_path,args["manifest_sha256"],None,view=view,deadline_ns=c.ADMISSION["work_ns"])
                observation["facts"]["actual_runtime_member_count"]=len(closure.holds)
                identities={key:a171_identity(os.fstat(blob.fd)) for key,blob in closure.holds.items()}
                return {"actual_runtime_member_count":len(closure.holds),
                    "actual_held_identity_set_sha256":hashlib.sha256(
                        json.dumps(identities,sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest(),
                    "runtime_OS_authority_credit":False}
            finally:
                if closure is not None:closure.close()
        if consumer=="Supervisor.seal":
            a171_keys(args,("parent","relative","sha256"))
            path=a171_path(a171_parent(args["parent"]),args["relative"])
            raw=a171_called(consumer,"Supervisor.seal",s.seal,path,args["sha256"],1048576)
            a171_result_bytes(raw,"actual_supervisor_sealed_bytes")
            return {"actual_bytes":len(raw),"actual_sha256":hashlib.sha256(raw).hexdigest()}
        if consumer=="Supervisor.NativeLaunchAdapter":
            a171_keys(args,("parent","relative","sha256"))
            path=a171_path(a171_parent(args["parent"]),args["relative"]);blob=None
            record=a171_fd_record(custody)
            try:
                blob=s.HeldSource(path,args["sha256"],262144,deadline_ns=c.ADMISSION["work_ns"])
                record["fd"]=blob.fd;record["identity9"]=a171_identity(os.fstat(blob.fd))
                # Only the legacy constructor's actual Root-file custody refusal.
                # No adapter handoff, ELF execution, new authority or weaker launch.
                a171_called(consumer,"Supervisor.NativeLaunchAdapter",s.NativeLaunchAdapter,
                    blob.fd,blob.fd,launcher_sha=args["sha256"],runtime_sha=args["sha256"],
                    deadline=None,deadline_ns=c.ADMISSION["work_ns"])
                raise c.Refused("A171_EXPECTED_ROOT_AUTHORITY_REFUSAL_DID_NOT_OCCUR")
            finally:
                if blob is not None:a171_close(record);blob.fd=None
        if consumer=="Supervisor.cgroup_values":
            a171_keys(args,("parent",))
            root=a171_parent(args["parent"])+"/cgroup"
            record=a171_fd_record(custody)
            keys={"memory.max","memory.swap.max","memory.oom.group","pids.max","cpu.max","cgroup.procs","cgroup.events"}
            try:
                record["fd"]=x.open_nf(root);record["identity9"]=a171_identity(os.fstat(record["fd"]))
                st=os.fstat(record["fd"])
                c.need(x.stat.S_ISDIR(st.st_mode) and st.st_uid==st.st_gid==os.getuid() and
                    x.stat.S_IMODE(st.st_mode)==0o700,"A171_ORIGINAL_OWNED_CGROUP_TEXT_DIRECTORY")
                actual={}
                def read(key):
                    c.need(key in keys,"A171_CGROUP_FIXED_KEYS")
                    leaf=a171_fd_record(custody)
                    try:
                        leaf["fd"]=os.open(key,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=record["fd"])
                        st=os.fstat(leaf["fd"]);leaf["identity9"]=a171_identity(st)
                        c.need(x.stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==os.getuid() and
                            st.st_nlink==1 and x.stat.S_IMODE(st.st_mode)==0o600 and st.st_size<=65536,
                            "A171_OWNED_CONTROL_TEXT_CUSTODY")
                        raw=os.read(leaf["fd"],65537);c.need(len(raw)<=65536,"CGROUP_READ_CAP")
                        actual[key]={"raw_hex":raw.hex(),"identity9":a171_identity(os.fstat(leaf["fd"]))}
                        return raw.decode("ascii","strict").strip()
                    finally:a171_close(leaf)
                value=a171_called(consumer,"Supervisor.cgroup_values",s.cgroup_values,read)
                observation["facts"]["actual_control_text_reads"]=actual
                return {"consumer_return":value,"kernel_cgroup_authority_credit":False}
            finally:a171_close(record)
        if consumer=="Supervisor.HeldSource.bytes":
            a171_keys(args,("parent","relative","sha256","mutation_hex") if case=="held_path_replaced_exact_bytes"
                else ("parent","relative","sha256"))
            path=a171_path(a171_parent(args["parent"]),args["relative"]);blob=None
            record=a171_fd_record(custody)
            try:
                named_before=a171_identity(os.stat(path,follow_symlinks=False))
                blob=s.HeldSource(path,args["sha256"],262144,deadline_ns=c.ADMISSION["work_ns"])
                record["fd"]=blob.fd;record["identity9"]=a171_identity(os.fstat(blob.fd))
                if case=="held_kernel_write_refused":
                    try:os.pwrite(blob.fd,b"owned-local-write",0)
                    except OSError as exc:
                        observation["facts"]["actual_kernel_write_errno"]=exc.errno
                        c.need(exc.errno==x.errno.EPERM,"A171_KERNEL_SEAL_WRITE_REFUSAL")
                    else:raise c.Refused("A171_KERNEL_SEAL_WRITE_ACCEPTED")
                if case=="held_path_replaced_exact_bytes":
                    # Independently selected inert replacement bytes, applied only to
                    # this Root-selected private input after real sealed-fd capture.
                    replacement=path+".a171-owned-replacement"
                    replacement9=a171_create_owned(replacement,
                        whole216_hex(args["mutation_hex"],262144),custody)
                    os.replace(replacement,path)
                    named_after=a171_identity(os.stat(path,follow_symlinks=False))
                    c.need(named_after[:2]!=named_before[:2] and named_after[:2]==replacement9[:2],
                        "A171_ACTUAL_POST_CAPTURE_PATH_REPLACEMENT")
                    observation["facts"]["post_capture_path_replacement_observed"]=True
                    observation["facts"]["post_capture_named_before_identity9"]=named_before
                    observation["facts"]["post_capture_named_after_identity9"]=named_after
                raw=a171_called(consumer,"Supervisor.HeldSource.bytes",blob.bytes,
                    262144,deadline_ns=c.ADMISSION["work_ns"])
                a171_result_bytes(raw,"actual_held_original_bytes")
                return {"actual_bytes":len(raw),"actual_sha256":hashlib.sha256(raw).hexdigest()}
            finally:
                if blob is not None:
                    a171_close(record);blob.fd=None
        if consumer=="Run.guard" and spec["expected"]["stage"] in ("work_boundary","terminal_reserve","create_fresh_browser3"):
            a171_keys(args,())
            observation["unavailable_reason"]="ORIGINAL_DEADLINE_PHASE_NOT_CONNECTED:"+case
            stage={"work1140_boundary":"work_boundary","hard1200_boundary":"terminal_reserve",
                "deadline_create_fresh_browser3":"create_fresh_browser3"}[case]
            a171_called(consumer,stage,run.guard,stage,stage=="terminal_reserve")
            return {"actual_clock_ns":str(time.monotonic_ns()),"original_work_ns":str(c.ADMISSION["work_ns"]),
                "original_hard_ns":str(c.ADMISSION["hard_ns"])}
        if consumer=="terminal_bytes":
            a171_keys(args,("value",))
            observation["unavailable_reason"]="ORIGINAL_SERIALIZER_FAILURE_CONTROL_NOT_CONNECTED:"+case
            # JSON-only input cannot invent a failing serializer callback. Its absence
            # is not converted to SERIALIZATION_FAULT or original sticky uncertainty.
            raw=a171_called(consumer,"terminal_serialization",x.terminal_bytes,args["value"],run)
            a171_result_bytes(raw,"actual_serialized_terminal")
            return {"actual_terminal_bytes":len(raw),"actual_terminal_sha256":hashlib.sha256(raw).hexdigest()}
        if consumer=="emit_terminal" and case in ("terminal_output_path","terminal_sink_blocked"):
            a171_keys(args,("value",));read_record=None;write_record=a171_fd_record(custody)
            try:
                if case=="terminal_output_path":
                    write_record["fd"]=os.memfd_create("friday-a171-owned-terminal",os.MFD_CLOEXEC)
                else:
                    read_record=a171_fd_record(custody)
                    read_fd,write_fd=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)
                    read_record["fd"]=read_fd;write_record["fd"]=write_fd
                    read_record["identity9"]=a171_identity(os.fstat(read_fd))
                    # Actual finite pipe capacity, not an asserted blocked-writer flag.
                    filled=0
                    while filled<1048576:
                        try:filled+=os.write(write_fd,b" "*4096)
                        except BlockingIOError:break
                    c.need(filled<1048576,"A171_ACTUAL_PIPE_CAPACITY_BOUND")
                    observation["facts"]["actual_prefill_bytes"]=filled
                write_record["identity9"]=a171_identity(os.fstat(write_record["fd"]))
                emitted=a171_called(consumer,"terminal_sink",x.emit_terminal,
                    args["value"],run,write_record["fd"])
                observation["state"]="TERMINAL_EMITTED" if emitted else "TERMINAL_EMISSION_FAILED"
                if read_record is not None:
                    a171_close(write_record);parts=[];seen=0
                    while seen<=1048576:
                        try:part=os.read(read_record["fd"],min(65536,1048577-seen))
                        except BlockingIOError:break
                        if not part:break
                        parts.append(part);seen+=len(part)
                    raw=b"".join(parts);c.need(len(raw)<=1048576,"A171_ACTUAL_SINK_RETAINED_CAP")
                    a171_result_bytes(raw,"actual_terminal_sink_retained_prefix")
                    observation["facts"]["actual_retained_sink_bytes"]=len(raw)
                    observation["facts"]["actual_retained_sink_sha256"]=hashlib.sha256(raw).hexdigest()
                return {"emitted":emitted,"actual_reason":run.reason}
            finally:
                a171_close(write_record)
                if read_record is not None:a171_close(read_record)
        if consumer=="Supervisor.write_terminal" and case=="outer_sink_blocked":
            a171_keys(args,("value",));read_record=a171_fd_record(custody);write_record=a171_fd_record(custody)
            try:
                read_fd,write_fd=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)
                read_record["fd"]=read_fd;write_record["fd"]=write_fd
                read_record["identity9"]=a171_identity(os.fstat(read_fd))
                write_record["identity9"]=a171_identity(os.fstat(write_fd))
                filled=0
                while filled<1048576:
                    try:filled+=os.write(write_fd,b" "*4096)
                    except BlockingIOError:break
                c.need(filled<1048576,"A171_ACTUAL_OUTER_PIPE_CAPACITY_BOUND")
                observation["facts"]["actual_outer_prefill_bytes"]=filled
                trace={}
                emitted=a171_called(consumer,"outer_terminal_sink",s.write_terminal,
                    args["value"],write_fd,trace,deadline_ns=c.ADMISSION["hard_ns"])
                observation["state"]="TERMINAL_EMITTED" if emitted else "TERMINAL_EMISSION_FAILED"
                observation["facts"]["actual_outer_terminal_trace"]=trace
                if trace.get("cause") is not None:
                    observation["first_fault"]={"cause":trace["cause"],"stage":"outer_terminal_sink",
                        "consumer":consumer,"at_ns":str(time.monotonic_ns())}
                a171_close(write_record);parts=[];seen=0
                while seen<=1048576:
                    try:part=os.read(read_fd,min(65536,1048577-seen))
                    except BlockingIOError:break
                    if not part:break
                    parts.append(part);seen+=len(part)
                actual=b"".join(parts);c.need(len(actual)<=1048576,"A171_ACTUAL_OUTER_RETAINED_CAP")
                a171_result_bytes(actual,"actual_outer_sink_retained_prefix")
                observation["facts"]["actual_retained_outer_sink_bytes"]=len(actual)
                observation["facts"]["actual_retained_outer_sink_sha256"]=hashlib.sha256(actual).hexdigest()
                return {"emitted":emitted,"actual_trace":trace}
            finally:a171_close(write_record);a171_close(read_record)
        raise A171CodeGap("ORIGINAL_OPERATION_PHASE_OR_APPROVED_DOMAIN_NOT_CONNECTED:"+case)
    finally:a171_merge_run(run)

def a171_validate_observation(value,meta,need,case):
    """Called separately by producer's original controller AND external A087 oracle."""
    need(type(value) is dict and value.get("schema")=="friday.a171.actual-operation-observation.v1",
        "A171_FULL_OPERATION_OBSERVATION")
    need(value["unavailable_reason"] is None,"A171_MANDATORY_CODE_GAP_NOT_A_PASS")
    need(value["resource_precondition"] is not None and value["resource_precondition"]["passed"] is True,
        "A171_ACTUAL_OPERATION_RESOURCE_PRECONDITION")
    need(not value["facts"].get("auxiliary_close_unconfirmed",False) and
        all((record["fd"] is None or (record["closed"] is True and record["close_attempted"] is True)) and
            record["close_error"] is None for record in value["descriptor_records"]),
        "A171_ACTUAL_OPERATION_DESCRIPTOR_CLEANUP")
    dependency_errors=[item for item in value["facts"].get("run_errors") or [] if item!=meta["cause"]]
    need(not dependency_errors and value["facts"].get("sticky_uncertainty") is not True,
        "A171_DEPENDENCY_CLEANUP_BLOCKS_SUCCESS")
    need(value["state"]==meta["state"] and value["started_routes"]==meta["started_routes"],
        "A171_ORIGINAL_STATE_STARTS")
    need(type(value["stages"]) is dict and value["stages"].get(meta["stage"],0)>0,
        "A171_ORIGINAL_STAGE_REACHED")
    calls=value["consumer_calls"]
    need(type(calls) is list and len(calls)>=meta["minimum_consumer_calls"] and
        all(type(call) is dict and (call["returned"] or call["error"] is not None) for call in calls),
        "A171_ACTUAL_ORIGINAL_CONSUMER_INVOCATIONS")
    aliases={"mapping_check/tokens/container/route_object":"mapping_check","mapping_check/container":"mapping_check"}
    wanted=aliases.get(meta["consumer"],meta["consumer"])
    need(sum(call["consumer"]==wanted for call in calls)>=meta["minimum_consumer_calls"],
        "A171_ACTUAL_REQUESTED_CONSUMER_INVOCATIONS")
    hashes=value["hashes"];children=value["target_children"]
    need(type(hashes) is list and type(children) is list and len(children)==meta["owned_children"],
        "A171_ORIGINAL_ACTUAL_TARGET_CHILDREN")
    if meta["hashes"]=="none":need(len(hashes)==0,"A171_ORIGINAL_NO_HASH")
    elif meta["hashes"]=="exact3":need(len(hashes)==3,"A171_ORIGINAL_EXACT3_HASH")
    elif meta["hashes"]=="at_least1":need(len(hashes)>=1,"A171_ORIGINAL_HASH_REACHED")
    elif meta["hashes"]=="stage_dependent":
        need(value["facts"].get("actual_stage_hash_relation") is True,"A171_STAGE_DEPENDENT_HASH_UNPROVEN")
    else:need(False,"A171_UNKNOWN_ORIGINAL_HASH_DOMAIN")
    fault=value["first_fault"]
    need(fault is None if meta["cause"] is None else type(fault) is dict and fault["cause"]==meta["cause"],
        "A171_ORIGINAL_ACTUAL_FIRST_FAULT")
    need(type(value["phase_order"]) is list and all(type(phase["at_ns"]) is str and phase["at_ns"].isdecimal()
        for phase in value["phase_order"]),"A171_ACTUAL_PHASE_ORDER")
    actual_times=[int(phase["at_ns"]) for phase in value["phase_order"]]
    need(actual_times==sorted(actual_times),"A171_ACTUAL_MONOTONIC_PHASE_ORDER")
    if case.startswith("outer_") or case.startswith("owned_registration_"):
        labels=[item.get("label") for item in value["operation_raw"]]
        need("outer_stdout" in labels and "outer_stderr" in labels,"A171_COMPLETE_OUTER_STREAMS_REQUIRED")
    for raw in value["operation_raw"]:
        if raw.get("transport")=="stdout_frame" and type(raw.get("raw_hex")) is not str:
            need(False,"A171_FULL_RAW_FRAME_NOT_RESTORED")
        actual=bytes.fromhex(raw["raw_hex"])
        need(raw["hash_only"] is False and len(actual)==raw["bytes"] and
            hashlib.sha256(actual).hexdigest()==raw["sha256"],"A171_FULL_OPERATION_RAW_HASH")
    # Positive source credit also needs per-ID controlled post-admission facts.
    # Their absence remains a CODE residual even if a simpler physical call returned.
    if case=="held_kernel_write_refused":
        need(value["facts"].get("actual_kernel_write_errno")==1,"A171_ACTUAL_ORIGINAL_KERNEL_SEAL_EPERRM")
    if case=="held_path_replaced_exact_bytes":
        need(value["facts"].get("post_capture_path_replacement_observed") is True,
            "A171_ACTUAL_POST_CAPTURE_PATH_CONTROL_REQUIRED")
