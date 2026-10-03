"""SOURCE ONLY. Ordinary public subset, not full registry control acceptance.

Future execution needs independently approved stock/native Root invocation,
protected runtime, source bytes and capsule pins. This file neither creates
those objects nor grants their authority. No code in this package ran in A079.
"""
import fcntl
import hashlib
import json
import os
import select
import signal
import stat
import struct
import time

SOURCES=(101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
CASES=("positive","wait_flags_DATA","caller_status_DATA")
PAYLOADS=tuple(("ordinary-owned-a079/%d\n"%i).encode("ascii") for i in range(3))

def need(ok,cause):
    if not ok:raise RuntimeError(cause)

def strict_json(raw):
    def unique(items):
        result={}
        for key,value in items:
            need(key not in result,"A079_DUPLICATE_DATA_KEY");result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _:(_ for _ in ()).throw(RuntimeError("A079_NONFINITE_DATA")))

def identity(st):
    return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

def generation(pid):
    fd=os.open("/proc/%d/stat"%pid,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:raw=os.read(fd,4097)
    finally:os.close(fd)
    need(0<len(raw)<=4096 and b")" in raw,"A079_ACTUAL_PROC")
    fields=raw.rsplit(b")",1)[1].split();need(len(fields)>=20,"A079_PROC_FIELDS")
    return int(fields[1]),int(fields[19])

def pidfd_pid(fd):
    read=os.open("/proc/self/fdinfo/%d"%fd,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:raw=os.read(read,4097)
    finally:os.close(read)
    need(len(raw)<=4096,"A079_PIDFD_BOUND")
    values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
    need(len(values)==1,"A079_ACTUAL_PIDFD");return values[0]

def inputs(bundle,case):
    """bundle=(actual FD map, independently fixed capsule SHA, full source SHA
    expectations). No expected pin is selected from the bytes under test."""
    fds,pin,expected=bundle
    need(case in CASES and set(fds)==set((100,111,120,121,122)+SOURCES) and
        set(expected)==set(SOURCES) and type(pin) is str and len(pin)==64,"A079_FULL_PUBLIC_INPUT_SET")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        fd=fds[role];st=os.fstat(fd);limit=16777216 if role in (110,111) else 1048576
        need(stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==0 and 0<=st.st_size<=limit and
            fcntl.fcntl(fd,fcntl.F_GET_SEALS)==15,"A079_ACTUAL_ROOT_SEALED_INPUT")
        raw=os.pread(fd,limit+1,0);need(len(raw)==st.st_size,"A079_FULL_INPUT_READ")
        sha=hashlib.sha256(raw).hexdigest()
        if role==100:need(sha==pin,"A079_INDEPENDENT_CAPSULE_PIN")
        elif role in SOURCES:need(sha==expected[role],"A079_INDEPENDENT_SOURCE_PIN")
        snapshots[role]=(identity(st),sha);raws[role]=raw
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A079_MODE1_FULL_CAPSULE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns()<work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A079_FIXED_180_10_LIFETIME")
    need(cap[112:144]!=b"\0"*32 and cap[144:176].hex()==snapshots[111][1] and
        cap[176:208].hex()==expected[117] and cap[208:240].hex()==expected[116] and
        all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A079_FULL_CAPSULE_SOURCE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A079_EXTERNAL_ACTUAL_ROOT_AND_GROUP_BINDING")
    data=strict_json(raws[119])
    need(data=={"schema":"friday.a079.ordinary-owned-native-input.v1","case":case,
        "workers":[{"payload":v.decode("ascii"),"exit":17+i} for i,v in enumerate(PAYLOADS)]},"A079_EXACT_INDEPENDENT_ORDINARY_INPUT_DATA")
    return snapshots,work,hard

def complement(bundle,snapshots):
    fds,_,_=bundle
    for role,(wanted,sha) in snapshots.items():
        limit=16777216 if role in (110,111) else 1048576
        need(identity(os.fstat(fds[role]))==wanted and
            hashlib.sha256(os.pread(fds[role],limit+1,0)).hexdigest()==sha and
            fcntl.fcntl(fds[role],fcntl.F_GET_SEALS)==15,"A079_EXACT_FULL_HELD_COMPLEMENT")

def oracle(outer,case):
    """Expectations are fixed here before the independent observed output."""
    need(outer.get("acceptance_complete") is False and outer.get("body_complete") is False and
        outer.get("coordinator_kernel_status_known") is True and outer.get("borrowed_status_kernel_credit") is False,
        "A079_NO_CALLER_STATUS_KERNEL_CREDIT")
    encoded=outer.get("native_inner_DATA_hex")
    need(type(encoded) is str and 0<len(encoded)<=32768,"A079_BOUNDED_ACTUAL_NATIVE_OUTPUT")
    raw=bytes.fromhex(encoded)
    need(raw.endswith(b"\n") and raw.count(b"\n")==1 and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A079_EXACT_ACTUAL_OUTPUT_BYTES_HASH_FRAME")
    inner=strict_json(raw)
    need(inner.get("schema")=="friday.a079.native-public-subset-result.v1" and inner.get("native_registry_only") is True and
        inner.get("case")==case and inner.get("all216")=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
        inner.get("full_authoritative_registry_controls")=="SOURCE_INCOMPLETE" and inner.get("acceptance_complete") is False and
        inner.get("current_GO") is False and inner.get("F10_waiver") is False,"A079_SUBSET_NOT_WHOLE_BROWSER")
    positive=case=="positive";rows=inner["ordinary_rows"]
    need(len(rows)==(3 if positive else 1) and outer["registered_workers"]==(3 if positive else 1) and
        outer["reaped_workers"]==(3 if positive else 0) and outer["registry_next_sequence"]==(13 if positive else 4),"A079_FULL_COUNT_AND_SEQUENCE_ORACLE")
    need(outer["state"]==("OUTER_BOUNDED_DRAINED_FINISHED" if positive else "STOP_UNCONFIRMED") and
        outer["reason"]==(None if positive else "COORD_EXIT") and outer["terminal_completion"] is positive and
        outer["uncertainty_sticky"] is (not positive),"A079_EXACT_OUTER_STATE_CAUSE_UNKNOWN")
    need(type(outer["aggregate_raw_RSS_peak_bytes"]) is int and 0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and
        0<outer["raw_self_peak_KiB"]<=65536 and 0<=outer["outer_memory_current"]<=67108864 and
        0<=outer["inner_memory_current"]<=201326592,"A079_ACTUAL_NATIVE_RESOURCE_ORACLE")
    before=inner["session_before"];after=inner["session_after"]
    need(before["uid"]==before["gid"]==1000 and before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2 and
        before["owner"]>0 and before["origin"]>0 and before["owner_birth"]>0 and before["origin_birth"]>0 and
        all(before[k]==after[k] for k in ("owner","origin","uid","gid","owner_birth","origin_birth")),"A079_FIXED_ORIGIN_COMPLEMENT")
    for role,row in enumerate(rows):
        initial,final=row["registered"],row["final"]
        need(row["role"]==role and row["payload"].encode("ascii")==PAYLOADS[role] and initial["pid"]>0 and initial["birth"]>0 and
            initial["state"]==2 and initial["wait_observed"]==initial["status_known"]==initial["cleanup_reaped"]==0 and
            all(initial[k]==final[k] for k in ("owner","origin","pid","role","birth","owner_birth","origin_birth")) and
            final["wait_observed"]==final["cleanup_reaped"]==final["handle_closed"]==1 and final["pidfd"]==-1,"A079_REGISTERED_READY_AND_EXACT_OWN_DISPOSAL")
        if positive:
            need(row["exit"]==17+role and final["state"]==3 and final["status_known"]==1 and final["status"]==((17+role)<<8) and
                final["creation_poisoned"]==0,"A079_INDEPENDENT_KERNEL_WAIT_AND_REAP_ACK")
        else:
            need(row["refusal_errno"]==(-22 if case=="wait_flags_DATA" else -1) and row["stop_errno"]==-117 and row["wait_status"] is None and
                final["state"]==4 and final["status_known"]==0 and final["status"]==0 and final["creation_poisoned"]==1,"A079_INERT_CALL_DATA_UNKNOWN_STATUS_ORACLE")
    return inner

# A158 normal held-entry custody; inlined into both actual ordinary drivers.
# The caller supplies only held descriptors and a separately selected pin.
class HeldAuxiliaries:
    def __init__(self):
        self.entries=[{"fd":None,"kind":None,"identity9":None,
            "attempted":False,"closed":False,"close_errno":None,"error":None} for _ in range(18)]
        self.metadata_next=7
    def own(self,slot,fd,kind):
        entry=self.entries[slot]
        entry["fd"]=fd;entry["kind"]=kind
        return fd
    def close(self,slot):
        entry=self.entries[slot]
        if entry["fd"] is None:return True
        if entry["attempted"]:return entry["closed"]
        entry["attempted"]=True
        try:os.close(entry["fd"])
        except BaseException as exc:
            entry["error"]=exc;entry["close_errno"]=getattr(exc,"errno",None) or type(exc).__name__
            return False
        entry["closed"]=True
        return True
    def metadata(self,path):
        if self.metadata_next>=18:raise RuntimeError("A158_METADATA_OPEN_BOUND")
        slot=self.metadata_next;self.metadata_next+=1
        fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
        self.own(slot,fd,"proc_metadata")
        primary=None
        try:
            self.entries[slot]["identity9"]=[str(v) for v in identity(os.fstat(fd))]
            raw=os.read(fd,4097)
            if not 0<len(raw)<=4096:raise RuntimeError("A158_METADATA_READ_BOUND")
            return raw
        except BaseException as exc:primary=exc;raise
        finally:
            if not self.close(slot) and primary is None:raise RuntimeError("A158_METADATA_CLOSE_UNCONFIRMED")
    def generation(self,pid):
        raw=self.metadata("/proc/%d/stat"%pid)
        if b")" not in raw:raise RuntimeError("A158_ACTUAL_PROC_FRAME")
        fields=raw.rsplit(b")",1)[1].split()
        if len(fields)<20:raise RuntimeError("A158_ACTUAL_PROC_FIELDS")
        return int(fields[1]),int(fields[19])
    def pidfd_pid(self,fd):
        raw=self.metadata("/proc/self/fdinfo/%d"%fd)
        values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
        if len(values)!=1:raise RuntimeError("A158_ACTUAL_PIDFD_IDENTITY")
        return values[0]
    def cleanup(self):
        for slot,entry in enumerate(self.entries):
            if entry["kind"]!="pidfd":self.close(slot)
    def receipt(self):
        return [dict(entry) for entry in self.entries if entry["fd"] is not None]

def held_error(exc):
    return None if exc is None else type(exc).__name__+":"+str(exc)[:512]

def held_capture(fds,pin,work,hard,*,main_reserve_ns,argpin=None,cleanup_reserve_ns=0):
    # Ledger and fixed custody state exist BEFORE the first real pipe. No PID,
    # raw stream, EOF or wait status is invented for a failed pre-clone entry.
    aux=HeldAuxiliaries()
    record={"pid":None,"pidfd":None,"birth":None,"owner":os.getpid(),
        "reaped":False,"status":None,"wait_status":None,"stop_attempted":False,
        "uncertainty_sticky":False,"handle_closed":False,"handle_identity9":None,
        "wait_error":None,"signal_error":None,"acquisition_stage":None}
    pairs=[];buffers=[bytearray(),bytearray()];hashers=[hashlib.sha256(),hashlib.sha256()]
    active=[False,False];seen=[0,0];eof=[False,False];overflow=[False,False]
    identities=[None,None];final_identities=[None,None]
    read_errors=[None,None];retention_errors=[None,None];close_errors=[None,None]
    pid=None;released=False;first_error=None;cleanup_errors=[];stage="FIRST_PIPE"
    def remember(exc):
        nonlocal first_error
        if first_error is None:first_error=exc
    def reap():
        if pid is None or record["reaped"] or record["wait_error"] is not None:return
        try:done,status=os.waitpid(pid,os.WNOHANG)
        except BaseException as exc:
            remember(exc);record["wait_error"]=exc;cleanup_errors.append("WAIT:"+held_error(exc));return
        if done==pid:record.update(reaped=True,status=status,wait_status=status,wait_ns=time.monotonic_ns())
    def drain():
        for index in range(2):
            if not active[index]:continue
            fd=aux.entries[2*index]["fd"]
            try:part=os.read(fd,65536)
            except BlockingIOError:continue
            except BaseException as exc:
                remember(exc);read_errors[index]=exc;active[index]=False
                if not aux.close(2*index):close_errors[index]=aux.entries[2*index]["close_errno"]
                continue
            if not part:
                eof[index]=True
                try:final_identities[index]=[str(v) for v in identity(os.fstat(fd))]
                except BaseException as exc:remember(exc);cleanup_errors.append("RAW_IDENTITY:"+held_error(exc))
                if not aux.close(2*index):close_errors[index]=aux.entries[2*index]["close_errno"]
                active[index]=False;continue
            seen[index]+=len(part)
            try:
                hashers[index].update(part)
                room=1048576-len(buffers[index]);buffers[index].extend(part[:room])
                if seen[index]>1048576:overflow[index]=True
            except BaseException as exc:
                remember(exc);retention_errors[index]=exc
                # Keep observing/draining the same pipe without claiming that
                # a failed retained prefix is the original complete stream.
    try:
        for index,kind in enumerate(("stdout","stderr","barrier")):
            pair=os.pipe2(os.O_CLOEXEC)
            aux.own(2*index,pair[0],kind+"_reader")
            aux.own(2*index+1,pair[1],kind+"_writer")
            pairs.append(pair)
        stage="PRECLONE_PIPE_METADATA"
        for index in range(2):
            fd=pairs[index][0]
            identities[index]=[str(v) for v in identity(os.fstat(fd))]
            aux.entries[2*index]["identity9"]=identities[index]
            os.set_blocking(fd,False);active[index]=True
        stage="ACTUAL_FORK"
        pid=os.fork()
        if pid==0:
            try:
                os.close(pairs[0][0]);os.close(pairs[1][0]);os.close(pairs[2][1])
                while time.monotonic_ns()<work:
                    if select.select([pairs[2][0]],[],[],.005)[0]:
                        if os.read(pairs[2][0],1)!=b"A":os._exit(124)
                        break
                else:os._exit(124)
                os.close(pairs[2][0])
                mapping={**fds,1:pairs[0][1],2:pairs[1][1]}
                copies={dst:fcntl.fcntl(src,fcntl.F_DUPFD_CLOEXEC,400) for dst,src in mapping.items()}
                for dst,src in copies.items():os.dup2(src,dst,inheritable=True)
                for src in copies.values():os.close(src)
                attach=os.open("cgroup.procs",os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=120)
                try:
                    value=str(os.getpid()).encode("ascii")
                    if os.write(attach,value)!=len(value):raise RuntimeError("A158_ACTUAL_SELF_ATTACH")
                finally:os.close(attach)
                for name in os.listdir("/proc/self/fd"):
                    fd=int(name)
                    if fd not in mapping:
                        try:os.close(fd)
                        except OSError:pass
                os.execve(110,["friday-approved-native-browser3","--held-a061",pin if argpin is None else argpin],
                    {"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"})
            except BaseException:os._exit(125)
            os._exit(125)
        # No parent endpoint close, allocation, proc or handle setup precedes
        # recording the exact actual fork result in this driver's own custody.
        record["pid"]=pid;stage="PARENT_ENDPOINT_CLOSE"
        for slot in (1,3,4):
            if not aux.close(slot):raise RuntimeError("A158_PARENT_ENDPOINT_CLOSE")
        stage="PARENT_PIDFD"
        record["pidfd"]=os.pidfd_open(pid,0);aux.own(6,record["pidfd"],"pidfd")
        record["handle_identity9"]=[str(v) for v in identity(os.fstat(record["pidfd"]))]
        parent,birth=aux.generation(pid);record["birth"]=birth
        if parent!=record["owner"] or aux.pidfd_pid(record["pidfd"])!=pid or aux.generation(pid)!=(parent,birth):
            raise RuntimeError("A158_ACTUAL_DIRECT_PARENT_GENERATION")
        stage="RELEASE"
        if os.write(pairs[2][1],b"A")!=1:raise RuntimeError("A158_ACTUAL_RELEASE")
        released=True
        if not aux.close(5):raise RuntimeError("A158_ACTUAL_RELEASE_CLOSE")
        stage="CONTINUOUS_DRAIN_REAP"
        while any(active) or not record["reaped"]:
            if time.monotonic_ns()>=hard-main_reserve_ns:raise RuntimeError("A158_ORIGINAL_MAIN_END")
            drain();reap()
            if first_error is not None:raise first_error
            if any(overflow):raise RuntimeError("A158_ORIGINAL_RAW_CAP")
            select.select([pairs[i][0] for i in range(2) if active[i]],[],[],.005)
    except BaseException as exc:
        remember(exc);record["acquisition_stage"]=stage
    finally:
        # EOF on this driver's unreleased barrier permits this exact child to
        # exit even when pidfd/proc setup failed. Signal has no guessed fallback.
        aux.close(5)
        if pid is not None and pid>0:
            reap()
            if released and not record["reaped"]:
                record["stop_attempted"]=True
                try:
                    handle=record["pidfd"]
                    if handle is None or aux.pidfd_pid(handle)!=pid or aux.generation(pid)!=(record["owner"],record["birth"]) or \
                        [str(v) for v in identity(os.fstat(handle))]!=record["handle_identity9"]:
                        raise RuntimeError("A158_SAME_ACTUAL_OWNED_HANDLE_REQUIRED")
                    signal.pidfd_send_signal(handle,signal.SIGKILL)
                except BaseException as exc:
                    record["signal_error"]=exc;cleanup_errors.append("SIGNAL:"+held_error(exc))
            end=min(hard-cleanup_reserve_ns,time.monotonic_ns()+10**9)
            while (any(active) or not record["reaped"]) and time.monotonic_ns()<end:
                drain();reap()
                try:select.select([pairs[i][0] for i in range(2) if active[i]],[],[],.005)
                except BaseException as exc:cleanup_errors.append("POLL:"+held_error(exc));break
            reap()
        aux.cleanup()
        if record["reaped"] and record["pidfd"] is not None:
            if aux.close(6):record.update(handle_closed=True,pidfd=None,close_ns=time.monotonic_ns())
        if any(not entry["closed"] for entry in aux.receipt()) or pid is not None and not record["reaped"]:
            record["uncertainty_sticky"]=True;cleanup_errors.append("STOP_UNCONFIRMED")
    if pid is None:
        return {"schema":"friday.a158.held-preclone-refusal.v1","accepted":False,"passed":False,
            "state":"STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_BEFORE_CLONE",
            "owned":record,"stdout_stream":None,"stderr_stream":None,
            "actual_auxiliary_cleanup":aux.receipt(),"original_error":first_error,"cleanup_errors":cleanup_errors}
    streams=[]
    for index in range(2):
        raw=None;freeze_error=None
        try:raw=bytes(buffers[index])
        except BaseException as exc:freeze_error=exc;remember(exc);retention_errors[index]=exc
        complete=eof[index] and not overflow[index] and read_errors[index] is None and retention_errors[index] is None and \
            close_errors[index] is None and raw is not None and seen[index]==len(raw)
        if not aux.entries[2*index]["closed"]:close_errors[index]=aux.entries[2*index]["close_errno"] or "UNKNOWN";complete=False
        raw_sha=None
        if raw is not None:
            try:raw_sha=hashlib.sha256(raw).hexdigest()
            except BaseException as exc:remember(exc);retention_errors[index]=exc;complete=False
        streams.append({"cap":1048576,"raw":raw,"retained_on_freeze_error":buffers[index] if freeze_error else None,
            "retained_size":len(buffers[index]),"total_seen":seen[index],"eof":eof[index],"overflow":overflow[index],
            "read_error":read_errors[index],"retention_error":retention_errors[index],"close_errno":close_errors[index],
            "pipe_identity9":identities[index],"pipe_final_identity9":final_identities[index],
            "sha256":raw_sha,
            "observed_sha256":hashers[index].hexdigest(),"hash_only":False,"original_stream_complete":complete,
            "full_original_bounded_raw":complete,"custody_domain":"same_actual_driver_original"})
    return {"schema":"friday.a158.held-raw-custody.v1","accepted":False,"passed":False,
        "state":"STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_SCOPED_RECEIPT",
        "owned":record,"stdout_stream":streams[0],"stderr_stream":streams[1],
        "actual_auxiliary_cleanup":aux.receipt(),"original_error":first_error,"cleanup_errors":cleanup_errors,
        "whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN","SourceReady":False,"GO":False}

def held_semantic_refusal(receipt,exc):
    if receipt["original_error"] is None:receipt["original_error"]=exc
    receipt["accepted"]=receipt["passed"]=False
    if receipt["state"]!="STOP_UNCONFIRMED":receipt["state"]="REFUSED_SCOPED_RECEIPT"
    return receipt
def run_public(bundle,case):
    snapshots,work,hard=inputs(bundle,case);fds,pin,_=bundle
    receipt=held_capture(fds,pin,work,hard,main_reserve_ns=10**9,cleanup_reserve_ns=100000000)
    receipt.update(case=case,network_effects=0,body_credit=False,whole_browser_credit=False,
        full_registry_controls="SOURCE_INCOMPLETE")
    if receipt["original_error"] is not None or receipt["cleanup_errors"] or receipt["stdout_stream"] is None:
        return receipt
    try:
        stdout,stderr=receipt["stdout_stream"],receipt["stderr_stream"]
        need(stdout["full_original_bounded_raw"] and stderr["full_original_bounded_raw"],"A079_FULL_ORIGINAL_RAW_CUSTODY")
        raw=stdout["raw"]
        need(not stderr["raw"] and raw.endswith(b"\n") and raw.count(b"\n")==1,"A079_NATIVE_TERMINAL_FRAME_NO_PREEMPTION")
        outer=strict_json(raw);receipt["outer"]=outer
        receipt["inner"]=oracle(outer,case)
        status=receipt["owned"]["status"]
        need(os.WIFEXITED(status) and os.WEXITSTATUS(status)==(0 if case=="positive" else 2),"A079_ACTUAL_PUBLIC_NATIVE_EXIT")
        complement(bundle,snapshots)
        receipt.update(accepted=True,passed=True,state="ACCEPTED_SCOPED_RECEIPT",terminal_sha256=stdout["sha256"])
    except BaseException as exc:return held_semantic_refusal(receipt,exc)
    return receipt


def run_positive_first(positive,negative,case):
    need(case in CASES[1:] and positive[1]!=negative[1],"A079_FRESH_INDEPENDENT_PAIR")
    a,b=(os.fstat(bundle[0][122]) for bundle in (positive,negative))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A079_FRESH_PROTECTED_VIEWS")
    need(all(positive[2][role]==negative[2][role] for role in SOURCES if role!=119),"A079_FULL_SOURCE_COMPLEMENT_BETWEEN_CASES")
    # A separate approved capsule binds each real view and single inert DATA
    # selector. No shared Root grant is invented from this pair or its result.
    pos=run_public(positive,"positive")
    if not pos["accepted"]:
        return {"positive":pos,"negative":None,"negative_disposition":"NOT_RUN_POSITIVE_REFUSED",
            "body_credit":False,"whole_browser_credit":False,"SourceReady":False,"GO":False}
    neg=run_public(negative,case)
    return {"positive":pos,"negative":neg,"source_ready":False,"current_GO":False,"full_registry_controls":"SOURCE_INCOMPLETE"}
