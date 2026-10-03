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
import os
import select
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
    # The protected native mode1 caller reaches a precise, persistent refusal.
    # Invented native ownership/cause labels cannot fulfill the 216 obligations.
    raise s.Refused("SOURCE_INCOMPLETE_INHERITED_NATIVE_CONTROLS_NOT_ADMITTED")
