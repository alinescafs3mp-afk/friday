"""Source only: qualified stock caller of actual held110 --held-a061.

The caller consumes independently supplied Root-selected typed admission and
oracle read-only descriptors. It cannot create Root, images, grants or receipts.
All its Source and cases are NOT_RUN. This corpus does not close the receiving
side Root-registry residual recorded in A087-EXPECTED-CONTROLS.json.
"""
import fcntl
import hashlib
import json
import os
import resource
import select
import signal
import stat
import struct
import time

SOURCES=(101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
PACKET=struct.Struct("<8s32sIIIIiiiiQQQ")
PAYLOADS=tuple(("ordinary-owned-a087/%d\n"%i).encode("ascii") for i in range(3))

def need(ok,cause):
    if not ok:raise RuntimeError(cause)

def json_DATA(raw):
    def unique(items):
        out={}
        for key,value in items:need(key not in out,"A087_DUPLICATE_KEY");out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(RuntimeError("A087_NONFINITE")))

def identity(st):
    return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

def proc(pid,custody=None):
    path="/proc/%d/stat"%pid
    if custody is None:fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW);slot=None
    else:fd,slot=custody.open_metadata(path)
    error=None
    try:raw=os.read(fd,4097)
    except BaseException as exc:error=exc;raise
    finally:
        if custody is None:os.close(fd)
        elif not custody.close(slot) and error is None:raise RuntimeError("A153_PROC_METADATA_CLOSE")
    need(0<len(raw)<=4096 and b")" in raw,"A087_ACTUAL_PROC")
    fields=raw.rsplit(b")",1)[1].split();need(len(fields)>=20,"A087_PROC_SIZE")
    return int(fields[1]),int(fields[19])

def pidfd_pid(fd,custody=None):
    path="/proc/self/fdinfo/%d"%fd
    if custody is None:read=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW);slot=None
    else:read,slot=custody.open_metadata(path)
    error=None
    try:raw=os.read(read,4097)
    except BaseException as exc:error=exc;raise
    finally:
        if custody is None:os.close(read)
        elif not custody.close(slot) and error is None:raise RuntimeError("A153_PIDFD_METADATA_CLOSE")
    need(len(raw)<=4096,"A087_FDINFO_BOUND")
    values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
    need(len(values)==1,"A087_ACTUAL_PIDFD");return values[0]

def pinned(fd,expected,maximum):
    need(type(fd) is int and fd>=0 and type(expected) is str and len(expected)==64 and
        all(v in "0123456789abcdef" for v in expected),"A087_EXTERNAL_PIN_TYPE")
    before=os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==0 and
        0<=before.st_size<=maximum and fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY and
        fcntl.fcntl(fd,fcntl.F_GET_SEALS)==15,"A087_ACTUAL_ROOT_READONLY_SEALED_INPUT")
    raw=os.pread(fd,before.st_size+1,0)
    need(len(raw)==before.st_size and hashlib.sha256(raw).hexdigest()==expected and identity(os.fstat(fd))==identity(before),
        "A087_COMPLETE_INDEPENDENT_PINNED_BYTES")
    return raw,(identity(before),expected)

def prepare(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case"} and
        bundle["schema"]=="friday.a087.stock-public-call.v1","A087_TYPED_PUBLIC_INPUT")
    fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A087_FULL_FD_ROLE_GRAPH")
    admission,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(admission)
    need(set(admission)=={"schema","expected_driver","resource_class","own_reply_helper","own_real_uid_transition","own_real_gid_transition",
        "own_local_syscall_restriction","Root_selected_expected_inputs"} and
        admission["schema"]=="friday.a087.external-stock-actor-admission.v1" and
        admission["resource_class"]=="ordinary180_reserve10_outer1_inner4_rss256MiB" and
        admission["Root_selected_expected_inputs"] is True,"A087_EXTERNAL_TYPED_ADMISSION")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A087_ACTUAL_SELECTED_STOCK_ACTOR")
    case=bundle["case"]
    if "origin_pid" in case:need(admission["own_reply_helper"] is True,"A087_SEPARATE_HELPER_ADMISSION")
    if "origin_uid" in case:need(admission["own_real_uid_transition"] is True,"A087_SEPARATE_REAL_UID_ADMISSION")
    if "origin_gid" in case:need(admission["own_real_gid_transition"] is True,"A087_SEPARATE_REAL_GID_ADMISSION")
    if case in ("signal_denied","close_denied"):need(admission["own_local_syscall_restriction"] is True,"A087_SEPARATE_OWN_RESTRICTION_ADMISSION")
    raw_oracle,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)
    matrix=json_DATA(raw_oracle)
    need(matrix["schema"]=="friday.a087.independent-public-oracles.v1" and matrix["full_scoped_cause_closed"] is False and
        matrix["source_ready"] is False and matrix["GO"] is False,"A087_EXPECTED_SCOPE_NO_WAIVER")
    matches=[row for row in matrix["rows"] if row["case"]==case];need(len(matches)==1,"A087_EXACT_PRESELECTED_CASE")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576)
        raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A087_PREREQUISITE_VALID_CAPSULE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A087_REAL_LIFETIME_AND_PREREQUISITE_HEADROOM")
    need(cap[112:144]!=b"\0"*32 and cap[144:176].hex()==bundle["image_sha256"] and
        cap[176:208].hex()==expected[117] and cap[208:240].hex()==expected[116] and
        all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A087_ENTIRE_CAPSULE_SOURCE_IMAGE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A087_ACTUAL_SELECTED_ROOT_GROUPS")
    ordinary={"schema":"friday.a087.native-public-input.v1","case":case,
        "workers":[{"payload":v.decode("ascii"),"exit":23+i} for i,v in enumerate(PAYLOADS)]}
    need(raws[119]==(json.dumps(ordinary,separators=(",",":"))+"\n").encode("ascii"),"A087_EXACT_CANONICAL_BENIGN_INPUT")
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    return snapshots,work,hard,matches[0]

def complement(bundle,snapshots):
    for role,value in snapshots.items():
        if type(role) is int:
            snap=value;fd=bundle["fds"][role]
        else:snap,fd=value
        raw,_=pinned(fd,snap[1],16777216 if role in (110,111) else 1048576)
        need(identity(os.fstat(fd))==snap[0],"A087_WHOLE_SAME_HELD_COMPLEMENT")

def evidence(trace,stage,errno,phase=None):
    need(trace["version"]==1 and trace["stage"]==stage and trace["primitive_errno"]==errno and
        (phase is None or trace["phase"]==phase) and len(bytes.fromhex(trace["expected"]))==96 and
        len(bytes.fromhex(trace["observed"]))==96,"A087_EXACT_ACTUAL_PRIMITIVE_PHASE_STAGE")
    need(trace["rights_close_errno"]==0 and trace["rights_closed"]==trace["rights"],"A087_ALL_ACTUAL_RECEIVED_RIGHTS_CLOSED")

def reply_oracle(trace,case,ctl,owned):
    need((trace["expected_pid"],trace["expected_uid"],trace["expected_gid"])==(owned["pid"],0,0),"A087_EXACT_EXPECTED_PID_UID_GID")
    expected=PACKET.unpack(bytes.fromhex(trace["expected"]));observed=PACKET.unpack(bytes.fromhex(trace["observed"]))
    if "lost_ACK" in case:
        need((trace["observed_pid"],trace["observed_uid"],trace["observed_gid"])==(-1,-1,-1) and
            bytes.fromhex(trace["observed"])==bytes(96),"A087_ACTUAL_LOST_REPLY_NO_FABRICATED_OBSERVATION");return
    need((trace["observed_pid"],trace["observed_uid"],trace["observed_gid"])==
        ((ctl["reply_pid"],ctl["reply_uid"],ctl["reply_gid"]) if ctl["reply_used"] else (owned["pid"],0,0)),"A087_ACTUAL_FULL_CONTROLLER_ORIGIN_CREDENTIALS")
    wanted=list(expected)
    if "owner_birth" in case:wanted[11]+=1
    elif "owner" in case:wanted[7]+=1
    if "session" in case:wanted[1]=bytes((expected[1][0]^1,))+expected[1][1:]
    if "role" in case:wanted[5]+=1
    if "sequence_replay" in case:wanted[4]-=1
    elif "sequence_future" in case or case=="start_sequence":wanted[4]+=1
    if "deadline" in case:wanted[12]-=1
    if "invalid_ACK" in case:wanted[3]=11
    need(observed==tuple(wanted),"A087_SINGLE_CONTROLLED_ARGUMENT_AND_EXACT_PACKET_COMPLEMENT")
    need(trace["rights"]==(2 if case=="ack_many_rights" else 1 if "rights" in case else 0),"A087_EXACT_ACTUAL_RIGHT_COUNT")

def oracle(outer,expected,owned,inner_stdout=None):
    case=expected["case"];positive=expected["completion"]
    need(outer["state"]==expected["outer_state"] and outer["reason"]==expected["outer_cause"] and
        outer["terminal_completion"] is positive and outer["uncertainty_sticky"] is expected["outer_uncertainty"] and
        outer["registered_workers"]==expected["root_registered_workers"] and outer["reaped_workers"]==expected["root_borrowed_reaped_workers"] and
        outer["registry_next_sequence"]==expected["root_sequence"] and outer["coordinator_kernel_status_known"] is True and
        outer["borrowed_status_kernel_credit"] is False and outer["body_complete"] is False and outer["acceptance_complete"] is False,
        "A087_FULL_ROOT_PHASE_COUNT_CAUSE_TERMINAL")
    ctl=outer["public_control"]
    need(ctl["case"]==case and ctl["origin_pid"]==owned["pid"] and ctl["origin_parent"]==owned["owner"] and
        ctl["origin_birth"]==owned["birth"] and ctl["coordinator_pid"]>0 and ctl["coordinator_birth"]>0,"A087_INDEPENDENT_ACTUAL_ORIGIN_BINDINGS")
    helper=1 if "origin_pid" in case else 0;plain=1 if "rights" in case else 0
    need(ctl["helper_created"]==ctl["helper_reaped"]==ctl["helper_closed"]==helper and
        ctl["plaintext_created"]==ctl["plaintext_closed"]==plain and
        ctl["credential_transitions"]==(1 if "origin_uid" in case or "origin_gid" in case else 0),"A087_REAL_HELPER_PLAINTEXT_CREDENTIAL_CLEANUP")
    encoded=outer["native_inner_DATA_hex"]
    if inner_stdout is None:
        need(type(encoded) is str and 0<len(encoded)<=32768,"A087_RAW_INNER_BOUND");raw=bytes.fromhex(encoded)
    else:
        need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576,"A118_RAW_INNER_BOUND");raw=inner_stdout
    need(raw.endswith(b"\n") and raw.count(b"\n")==1 and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A087_EXACT_BYTES_FRAME_AND_DIGEST")
    inner=json_DATA(raw)
    need(inner["schema"]=="friday.a087.native-public-result.v1" and inner["case"]==case and inner["native_registry_only"] is True and
        inner["terminal_completion"] is positive and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["current_GO"] is False and inner["F10_waiver"] is False and inner["all216"]=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
        inner["full_authoritative_registry_controls"]=="SOURCE_INCOMPLETE","A087_SOURCE_RESIDUAL_NO_BROWSER_CREDIT")
    resources=inner["resources"]
    need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["NOFILE"]==[512,512] and
        resources["FSIZE"]==[2147483648,2147483648] and resources["CORE"]==[0,0] and
        0<resources["self_peak_bytes"]<=50331648 and 0<=resources["children_peak_bytes"]<=201326592 and
        type(outer["aggregate_raw_RSS_peak_bytes"]) is int and 0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and
        0<outer["raw_self_peak_KiB"]*1024<=67108864 and 0<=outer["outer_memory_current"]<=67108864 and
        0<=outer["inner_memory_current"]<=201326592,"A087_ACTUAL_COMPLETE_RESOURCE_ORACLE")
    need(outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456,"A087_CALLER_INCLUDED_RSS_CAP")
    if case.startswith("start_"):
        need(inner["phase"]=="START" and inner["refusal_errno"]==expected["expected_primitive_errno"] and inner["ordinary_rows"]==[],"A087_START_CAUSE_NO_CHILD")
        trace=inner["evidence"];evidence(trace,expected["expected_native_stage"],expected["expected_primitive_errno"],1)
        need(trace["expected_pid"]==owned["pid"] and trace["expected_uid"]==trace["expected_gid"]==0,"A087_FULL_EXPECTED_ORIGIN")
        reply_oracle(trace,case,ctl,owned)
        return inner
    before,after=inner["session_before"],inner["session_after"]
    resources=inner["resources"]
    need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["NOFILE"]==[512,512] and
        resources["FSIZE"]==[2147483648,2147483648] and resources["CORE"]==[0,0] and 0<resources["self_peak_bytes"]<=50331648 and
        0<=resources["children_peak_bytes"]<=201326592 and resources["affinity"]==sorted(os.sched_getaffinity(0)),"A091_OWNER_CALLER_REAL_ORIGINAL_RESOURCE_ENVELOPE")
    need(before["origin"]==owned["pid"] and before["origin_birth"]==owned["birth"] and
        before["owner"]==ctl["coordinator_pid"] and before["owner_birth"]==ctl["coordinator_birth"] and
        before["uid"]==before["gid"]==1000 and before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2 and
        after["next_sequence"]==expected["native_sequence_before_drain_finish"] and
        all(before[k]==after[k] for k in ("owner","origin","uid","gid","owner_birth","origin_birth")),"A087_NATIVE_PRIVATE_FIXED_ORIGIN_SEQUENCE")
    rows=inner["ordinary_rows"]
    need(sum(row["registered"]["pid"]>0 for row in rows)==expected["actual_children_created"] and
        sum(row["ready"] is not None for row in rows)==expected["ordinary_READY_count"],"A087_REAL_CREATION_READY_COUNTS")
    if not positive or case=="abort_positive":
        primary=inner["primary"];need(primary["result"]==expected["expected_primitive_errno"],"A087_EXACT_PRIMARY_CALL_RESULT")
        evidence(primary["evidence"],expected["expected_native_stage"],0 if case=="abort_positive" else expected["expected_primitive_errno"],expected["expected_phase"])
        if case.startswith(("ack_","register_","abort_","reap_")):reply_oracle(primary["evidence"],case,ctl,owned)
    for role,row in enumerate(rows):
        initial,final=row["registered"],row["final"];trace=row["final_evidence"]
        need(initial["status_known"]==initial["wait_observed"]==0 and
            all(initial[k]==final[k] for k in ("owner","origin","pid","role","birth","owner_birth","origin_birth")),"A087_NO_PID_PARENT_GENERATION_ADOPTION")
        if initial["pid"]<=0:
            need(row["ready"] is None and final["state"]==4 and final["status_known"]==0,"A087_PRECLONE_REFUSAL");continue
        if row["ready"] is not None:
            ready_ns,pid,parent,uid,gid,birth,parent_birth=row["ready"]
            need((pid,parent,uid,gid,birth,parent_birth)==(initial["pid"],initial["owner"],1000,1000,initial["birth"],initial["owner_birth"]) and
                row["payload"].encode("ascii")==PAYLOADS[role] and row["spawn_evidence"]["ready_released"]==1 and
                0<row["spawn_evidence"]["intent_ack_ns"]<=row["spawn_evidence"]["clone_ns"]<=row["spawn_evidence"]["register_send_ns"]<=
                row["spawn_evidence"]["register_ack_ns"]<=row["spawn_evidence"]["release_ns"]<=ready_ns,"A087_ACTUAL_REGISTER_ACK_BEFORE_READY")
        else:need(trace["ready_released"]==0 and trace["register_ack_ns"]==0,"A087_NO_EARLY_BODY_OR_READY")
        need(final["wait_observed"]==final["cleanup_reaped"]==1 and trace["wait4_ns"]>0,"A087_ACTUAL_PRIVATE_WAIT_BEFORE_TERMINAL")
        lost_handle=case in ("pidfd_plaintext","pidfd_closed","close_denied")
        need(final["handle_closed"]==(0 if lost_handle else 1) and (lost_handle or final["pidfd"]==-1),"A087_PRECISE_CONFIRMED_OR_UNCONFIRMED_HANDLE_CLOSE")
        if positive:
            need(row["wait_result"]==1 and row["release_result"]==0 and final["state"]==3 and final["status_known"]==1 and
                final["status"]==(23+role)<<8 and 0<trace["wait4_ns"]<=trace["reap_ack_ns"]<=trace["close_ns"],"A087_INDEPENDENT_PRIVATE_KERNEL_EXIT_AND_ACK")
        else:need(final["state"]==4 and final["status_known"]==0 and final["status"]==0 and final["creation_poisoned"]==1 and
            trace["status_redacted"]==1 and PACKET.unpack(bytes.fromhex(trace["expected"]))[8]==PACKET.unpack(bytes.fromhex(trace["observed"]))[8]==0,
            "A087_STICKY_UNKNOWN_NEVER_PUBLIC_OR_DATA_STATUS")
    if not positive:
        again=inner["attempts"][-1];need(again["operation"]=="after_refusal_creation" and again["result"]==-117 and again["created_pid"]==0,"A087_NO_RESET_OR_RETRY_CREATION")
    if case=="register_delayed_ACK":need(rows[0]["spawn_evidence"]["register_ack_ns"]-rows[0]["spawn_evidence"]["register_send_ns"]>=100000000,"A087_REAL_ACK_DELAY_BARRIER")
    if case.startswith("reap_"):need(inner["primary"]["record"]["wait_observed"]==1 and inner["primary"]["record"]["status_known"]==0 and
        inner["primary"]["evidence"]["wait4_ns"]>0 and inner["primary"]["evidence"]["reap_ack_ns"]==0,"A087_WAIT4_STAYS_PRIVATE_AFTER_INVALID_OR_LOST_ACK")
    if case=="signal_denied":need(rows[0]["final_evidence"]["signal_attempts"]==1,"A087_ONE_REAL_SIGNAL_NO_RETRY")
    if case=="close_denied":need(rows[0]["final_evidence"]["close_attempts"]==1 and rows[0]["final_evidence"]["close_ns"]==0,"A087_FAILED_CLOSE_NOT_CONFIRMED")
    if case=="transport_closed":need(inner["transport_evidence"]["transport_close_attempts"]==1 and inner["transport_evidence"]["transport_close_ns"]>0,"A087_REAL_OWN_TRANSPORT_CLOSE")
    return inner

STREAM_CAP = 1048576

class PublicPreCloneRefused(RuntimeError):
    def __init__(self,cause,cleanup,original_error):
        super().__init__(cause)
        self.original_error=original_error
        self.receipt={"schema":"friday.a118.pre-clone-refusal.v1","cause":cause,"native_process_created":False,
            "native_streams":None,"owned_process":None,"actual_auxiliary_cleanup":cleanup,
            "original_error":original_error,"original_error_type":type(original_error).__name__,
            "primitive_errno":getattr(original_error,"errno",None),
            "state":"STOP_UNCONFIRMED" if any(not v["closed"] for v in cleanup) else "REFUSED_PRECLONE",
            "SourceReady_granted_here":False,"GO":False}

class PublicAuxiliaries:
    """Allocate the ledger before its first resource. A failed close is never
    silently marked closed or retried against a potentially reused FD."""
    def __init__(self):
        self.pairs=[None]*5
        self.entries=[{"slot":i,"kind":"metadata" if i>=10 else "barrier" if i>=8 else "output",
            "fd":None,"identity9":None,"close_attempts":0,"closed":False,
            "close_errno":None,"close_error_type":None,"original_error":None,"close_ns":None} for i in range(18)]
    def acquire(self):
        for i in range(5):
            pair=os.pipe2(os.O_CLOEXEC)
            self.pairs[i]=pair
            self.entries[2*i]["fd"]=pair[0];self.entries[2*i+1]["fd"]=pair[1]
    def open_metadata(self,path):
        # At most six existing proc/fdinfo acquisitions occur in this finite
        # driver generation. Track each actual FD immediately, before fstat.
        slot=next((i for i in range(10,18) if self.entries[i]["fd"] is None),None)
        need(slot is not None,"A153_FINITE_METADATA_LEDGER")
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        self.entries[slot]["fd"]=fd
        self.entries[slot]["identity9"]=[str(v) for v in identity(os.fstat(fd))]
        return fd,slot
    def close(self,slot):
        entry=self.entries[slot]
        if entry["fd"] is None:return True
        if entry["close_attempts"]:return entry["closed"]
        entry["close_attempts"]=1
        try:os.close(entry["fd"])
        except BaseException as exc:
            entry["close_errno"]=getattr(exc,"errno",None);entry["close_error_type"]=type(exc).__name__
            entry["original_error"]=exc
            return False
        entry["closed"]=True;entry["close_errno"]=0;entry["close_ns"]=time.monotonic_ns()
        return True
    def cleanup(self):
        for slot in range(len(self.entries)):self.close(slot)
    def receipt(self):return [entry for entry in self.entries if entry["fd"] is not None]

def public_pipes(custody):
    custody.acquire()
    return custody.pairs[:4],custody.pairs[4]

def public_error(exc):
    return str(exc) if isinstance(exc,RuntimeError) else "%s:%s"%(type(exc).__name__,getattr(exc,"errno",None))

def run_public(bundle):
    receiving=bundle.get("schema")=="friday.a091.stock-public-call.v1"
    whole216=bundle.get("schema")=="friday.a158.whole216-stock-call.v1"
    snapshots,work,hard,expected=prepare_whole216(bundle) if whole216 else prepare_receiving(bundle) if receiving else prepare(bundle)
    fds=bundle["fds"]
    # Four independent pipes keep every original stream at its original 1MiB
    # cap. Inner bytes are not duplicated or hex-expanded into the outer frame.
    auxiliaries=PublicAuxiliaries()
    pipe_identities=[None]*4;pipe_final_identities=[None]*4;active=[False]*4
    buffers=[None]*4;digests=[None]*4;seen=[0]*4;eof=[False]*4;overflow=[False]*4
    read_errors=[None]*4;close_errors=[None]*4;retention_errors=[None]*4
    causes=[];cleanup_errors=[];outer=inner=consumption=None
    driver=None;pid=None;first_error=None;stage="A153_PIPE_ACQUISITION";released=False
    owned={"owner":os.getpid(),"pid":None,"pidfd":None,"birth":None,"reaped":False,
        "kernel_status":None,"handle_closed":False,"signal_attempts":0,"signal_errno":0,
        "close_errno":0,"wait_ns":0,"close_ns":0,"pidfd_identity9":None,
        "handle_acquired":False,"wait_error":None,"acquisition_stage":None}
    def reap():
        nonlocal first_error
        if pid is not None and not owned["reaped"] and owned["wait_error"] is None:
            try:done,status=os.waitpid(pid,os.WNOHANG)
            except BaseException as exc:
                if first_error is None:first_error=exc
                owned["wait_error"]=public_error(exc);cleanup_errors.append("WAIT:"+owned["wait_error"]);return
            if done==pid:owned.update(reaped=True,kernel_status=status,wait_ns=time.monotonic_ns())
    def drain():
        nonlocal first_error
        # Each actual reader is nonblocking before clone. One refused reader
        # cannot prevent finite drainage/custody of the remaining real streams.
        for index in range(4):
            if not active[index]:continue
            fd=auxiliaries.entries[2*index]["fd"]
            try:part=os.read(fd,65536)
            except BlockingIOError:continue
            except BaseException as exc:
                if first_error is None:first_error=exc
                read_errors[index]=getattr(exc,"errno",None) or type(exc).__name__
                causes.append("READ:"+public_error(exc));active[index]=False
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
                continue
            if not part:
                eof[index]=True
                try:pipe_final_identities[index]=[str(v) for v in identity(os.fstat(fd))]
                except BaseException as exc:cleanup_errors.append("PIPE_FINAL_IDENTITY:"+public_error(exc))
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
                active[index]=False;continue
            try:
                seen[index]+=len(part);digests[index].update(part)
                room=STREAM_CAP-len(buffers[index]);buffers[index].extend(part[:room])
                if seen[index]>STREAM_CAP:overflow[index]=True
            except BaseException as exc:
                if first_error is None:first_error=exc
                retention_errors[index]=public_error(exc);causes.append("RETENTION:"+retention_errors[index])
                active[index]=False
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
    try:
        pairs,barrier=public_pipes(auxiliaries)
        stage="A153_PIPE_METADATA"
        for index,pair in enumerate(pairs):
            pipe_identities[index]=identity(os.fstat(pair[0]))
            auxiliaries.entries[2*index]["identity9"]=[str(v) for v in pipe_identities[index]]
            os.set_blocking(pair[0],False);active[index]=True
        stage="A153_DRIVER_BIRTH"
        parent,birth=proc(os.getpid(),auxiliaries)
        driver={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()}
        stage="A153_STREAM_ALLOCATION"
        for index in range(4):buffers[index]=bytearray();digests[index]=hashlib.sha256()
        stage="A153_OUTER_CLONE"
        pid=os.fork()
        if pid==0:
            # Setup errors terminate this actual child branch. They never
            # unwind into the driver's entry point or manufacture a receipt.
            try:
                for read,_ in pairs:os.close(read)
                os.close(barrier[1])
                while time.monotonic_ns()<work:
                    if select.select([barrier[0]],[],[],.002)[0]:
                        if os.read(barrier[0],1)!=b"A":os._exit(124)
                        break
                else:os._exit(124)
                os.close(barrier[0])
                mapping={**fds,1:pairs[0][1],2:pairs[1][1],128:pairs[2][1],129:pairs[3][1]}
                copies={dst:fcntl.fcntl(src,fcntl.F_DUPFD_CLOEXEC,400) for dst,src in mapping.items()}
                for dst,src in copies.items():os.dup2(src,dst,inheritable=True)
                for src in copies.values():os.close(src)
                attach=os.open("cgroup.procs",os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=120)
                try:need(os.write(attach,str(os.getpid()).encode("ascii"))>0,"A087_ACTUAL_SELF_ATTACH")
                finally:os.close(attach)
                for name in os.listdir("/proc/self/fd"):
                    fd=int(name)
                    if fd not in mapping:
                        try:os.close(fd)
                        except OSError:pass
                os.execve(110,["friday-approved-native-browser3","--held-a061",bundle["capsule_sha256"]],
                    {"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"})
            except BaseException:os._exit(125)
            os._exit(125)
        # The actual fork return is recorded before every parent acquisition.
        owned["pid"]=pid;stage="A153_PARENT_ENDPOINT_CLOSE"
        for index in range(4):need(auxiliaries.close(2*index+1),"A153_PARENT_WRITER_CLOSE")
        need(auxiliaries.close(8),"A153_PARENT_BARRIER_READER_CLOSE")
        stage="A153_OWNED_PIDFD"
        owned["pidfd"]=os.pidfd_open(pid,0);owned["handle_acquired"]=True
        parent,birth=proc(pid,auxiliaries);owned["birth"]=birth
        owned["pidfd_identity9"]=[str(v) for v in identity(os.fstat(owned["pidfd"]))]
        need(parent==owned["owner"] and pidfd_pid(owned["pidfd"],auxiliaries)==pid and proc(pid,auxiliaries)==(parent,birth),
            "A087_ACTUAL_OWNED_ORIGIN_BEFORE_RELEASE")
        stage="A153_OUTER_RELEASE"
        need(os.write(barrier[1],b"A")==1,"A087_PUBLIC_RELEASE");released=True
        need(auxiliaries.close(9),"A153_PUBLIC_RELEASE_CLOSE")
        stage="A153_CONTINUOUS_DRAIN"
        while any(active) or not owned["reaped"]:
            need(time.monotonic_ns()<hard-10**9,"A087_FINITE_CONTINUOUS_DRAIN")
            drain();reap()
            need(not causes and owned["wait_error"] is None,"A153_DRAIN_OR_WAIT_REFUSED")
            need(not any(overflow),"A087_OUTPUT_CAP")
            select.select([pairs[i][0] for i in range(4) if active[i]],[],[],.002)
    except BaseException as exc:
        if first_error is None:first_error=exc
        owned["acquisition_stage"]=stage;causes.append(stage+":"+public_error(exc))
    finally:
        # Closing the actual unreleased barrier permits finite child exit even
        # when proc/pidfd acquisition failed. No guessed PID is used to signal.
        auxiliaries.close(9)
        if pid is not None and pid>0:
            reap()
            if released and not owned["reaped"]:
                try:
                    need(owned["pidfd"] is not None and pidfd_pid(owned["pidfd"],auxiliaries)==pid and
                        proc(pid,auxiliaries)==(owned["owner"],owned["birth"]) and
                        [str(v) for v in identity(os.fstat(owned["pidfd"]))]==owned["pidfd_identity9"],
                        "A087_EXACT_OWN_HANDLE_BEFORE_SIGNAL")
                    owned["signal_attempts"]+=1
                    try:signal.pidfd_send_signal(owned["pidfd"],signal.SIGTERM)
                    except OSError as exc:owned["signal_errno"]=exc.errno;raise
                except BaseException as exc:cleanup_errors.append("SIGNAL:"+public_error(exc))
            stop=min(hard-100000000,time.monotonic_ns()+10**9)
            while (any(active) or not owned["reaped"]) and time.monotonic_ns()<stop:
                try:drain()
                except BaseException as exc:cleanup_errors.append("DRAIN:"+public_error(exc));break
                reap()
                try:
                    select.select([pairs[i][0] for i in range(4) if active[i]],[],[],.002)
                except BaseException as exc:cleanup_errors.append("POLL:"+public_error(exc));break
            reap()
        auxiliaries.cleanup()
        for index in range(4):
            if auxiliaries.entries[2*index]["fd"] is not None and not auxiliaries.entries[2*index]["closed"]:
                close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
        # A live exact handle remains owned by this same driver on an uncertain
        # stop. It is never transferred to a new process or silently discarded.
        if owned["reaped"] and owned["pidfd"] is not None:
            try:os.close(owned["pidfd"])
            except BaseException as exc:owned["close_errno"]=getattr(exc,"errno",None) or "UNKNOWN"
            else:owned.update(handle_closed=True,close_ns=time.monotonic_ns(),pidfd=None)
        if pid is not None and (not owned["reaped"] or
            owned["handle_acquired"] and not owned["handle_closed"] or
            any(not entry["closed"] for entry in auxiliaries.receipt())):
            cleanup_errors.append("A087_STOP_UNCONFIRMED")
    if pid is None:
        raise PublicPreCloneRefused(stage+":"+public_error(first_error),auxiliaries.receipt(),first_error) from first_error
    streams=[]
    for i,buf in enumerate(buffers):
        raw=bytes(buf)
        transfer_complete=(eof[i] and not overflow[i] and read_errors[i] is None and
            retention_errors[i] is None and seen[i]==len(raw))
        streams.append({"cap":STREAM_CAP,"size":seen[i],"retained_size":len(raw),"eof":eof[i],
            "overflow":overflow[i],"raw":raw,"sha256":hashlib.sha256(raw).hexdigest(),
            "observed_sha256":digests[i].hexdigest(),"observed_sha_complete":eof[i] and read_errors[i] is None,
            "prefix_hex":raw[:64].hex(),"hash_only":False,"read_errno":read_errors[i],
            "retention_error":retention_errors[i],"close_errno":close_errors[i],
            "pipe_identity9":[str(v) for v in pipe_identities[i]],
            "pipe_final_identity9":pipe_final_identities[i],
            "transfer_complete":transfer_complete,"transfer_eof":eof[i],"transfer_overflow":overflow[i],
            "stream_domain":"outer_original" if i<2 else "inner_transfer",
            "native_original":None,"native_original_binding_error":None,
            "original_stream_complete":transfer_complete if i<2 else None,
            "full_original_bounded_raw":transfer_complete if i<2 else None})
    try:
        self_usage=resource.getrusage(resource.RUSAGE_SELF);children_usage=resource.getrusage(resource.RUSAGE_CHILDREN)
        driver_resources={"self_peak_bytes":self_usage.ru_maxrss*1024,"children_historical_peak_bytes":children_usage.ru_maxrss*1024,
            "affinity":sorted(os.sched_getaffinity(0)),"AS":list(resource.getrlimit(resource.RLIMIT_AS)),
            "CPU":list(resource.getrlimit(resource.RLIMIT_CPU)),"NOFILE":list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "FSIZE":list(resource.getrlimit(resource.RLIMIT_FSIZE)),"CORE":list(resource.getrlimit(resource.RLIMIT_CORE))}
    except OSError:driver_resources=None
    receipt={"schema":"friday.a118.actual-public-receipt.v1","case":bundle["case"],"accepted":False,
        "outer":None,"inner":None,"driver":driver,"owned":owned,"stdout_sha256":streams[0]["sha256"],
        "stdout_stream":streams[0],"stderr_stream":streams[1],
        "inner_stdout_stream":streams[2],"inner_stderr_stream":streams[3],
        "causes":causes,"cleanup_errors":cleanup_errors,"ordinary_consumption":None,"driver_resources":driver_resources,
        "original_error":first_error,"actual_auxiliary_cleanup":auxiliaries.receipt(),
        "whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN",
        "bindings":{"capsule_sha256":bundle["capsule_sha256"],"source_sha256":dict(bundle["source_sha256"]),
            "image_sha256":bundle["image_sha256"],"source_manifest_sha256":bundle.get("source_manifest_sha256")},
        "source_ready":False,"current_GO":False,"whole_native_cause_closed":False,"body_credit":False,"F10_waiver":False}
    # Output custody is finalized before any semantic, frame, kernel-exit or
    # complement check. Every later refusal returns this same raw receipt.
    # Bind physical original-stream facts even on prior startup/drain refusal.
    # Transfer EOF alone cannot assert anything about the inner original EOF.
    try:
        raw=streams[0]["raw"]
        need(streams[0]["transfer_complete"] and raw.endswith(b"\n") and raw.count(b"\n")==1,
            "A087_FULL_PUBLIC_OUTPUT_FRAME")
        outer=json_DATA(raw);receipt["outer"]=outer
        bind_inner_streams(outer,streams[2:])
    except BaseException as exc:
        if receipt["original_error"] is None:receipt["original_error"]=exc
        causes.append(public_error(exc))
    if not causes and not cleanup_errors:
        try:
            need(all(v["full_original_bounded_raw"] is True and v["close_errno"] is None for v in streams),
                "A118_FULL_FOUR_STREAM_EOF")
            need(not streams[1]["raw"],"A087_PUBLIC_STDERR")
            inner=oracle_whole216(outer,expected,owned,streams[2]["raw"]) if whole216 else oracle_receiving(outer,expected,owned,streams[2]["raw"]) if receiving else oracle(outer,expected,owned,streams[2]["raw"])
            receipt["inner"]=inner
            need(os.WIFEXITED(owned["kernel_status"]) and os.WEXITSTATUS(owned["kernel_status"])==expected["outer_exit"],
                "A087_ACTUAL_OWN_KERNEL_WAIT_STATUS")
            complement(bundle,snapshots)
            if receiving and bundle["case"]=="positive":
                receipt["ordinary_consumption"]=consume_connected_positive(receipt,expected,snapshots)
            if whole216:
                receipt["ordinary_consumption"]=consume_whole216(receipt,expected,snapshots)
            receipt["accepted"]=True
        except BaseException as exc:
            if receipt["original_error"] is None:receipt["original_error"]=exc
            causes.append(public_error(exc))
    receipt["state"]="ACCEPTED_SCOPED_RECEIPT" if receipt["accepted"] else "STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_SCOPED_RECEIPT"
    return receipt

def run_positive_first(positive,controlled):
    need(positive["case"]=="positive" and controlled["case"]!="positive" and positive["capsule_sha256"]!=controlled["capsule_sha256"],"A087_FRESH_POSITIVE_FIRST_PAIR")
    # Actual OS/runtime-index/capsule bindings can differ between independently
    # selected views; all program/consumer Source pins must remain identical.
    need(all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)) and
        positive["oracle_sha256"]==controlled["oracle_sha256"],"A087_PROGRAM_AND_INDEPENDENT_ORACLE_COMPLEMENT")
    a,b=(os.fstat(v["fds"][122]) for v in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A087_FRESH_ACTUAL_PROTECTED_VIEWS")
    ordinary=run_public(positive)
    if not ordinary["accepted"]:return {"positive":ordinary,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED","source_ready":False,"current_GO":False}
    negative=run_public(controlled)
    return {"positive":ordinary,"controlled":negative,"full_scoped_cause_closed":False,"source_ready":False,"current_GO":False}

RECEIVING_CASES=("positive","peer_pid","peer_uid","peer_gid","origin_parent_argument","origin_birth_argument","owner","owner_birth",
    "frame_session","version","type","role","sequence_replay","sequence_future","deadline","intent_right",
    "register_zero_rights","register_many_rights","register_plaintext","register_other_owned_pidfd","register_parent",
    "register_birth","register_stale_pidfd","register_without_intent","register_late","abort_without_intent","abort_detail",
    "abort_right","abort_positive","reap_live","reap_birth","reap_pid","reap_status","reap_right","reap_invalid_ACK",
    "reap_lost_ACK","pending_timeout","drain_pending","drain_live","finish_without_drain","transport_closed",
    "Root_signal_positive","Root_signal_denied","Root_transport_close_denied","inherited_start_owner")

def prepare_receiving(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case","source_manifest_fd","source_manifest_sha256"} and
        bundle["schema"]=="friday.a091.stock-public-call.v1" and bundle["case"] in RECEIVING_CASES,"A091_TYPED_PUBLIC_INPUT")
    case=bundle["case"];fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A091_EXACT19_ROLE_GRAPH")
    raw_admission,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(raw_admission)
    need(set(admission)=={"schema","expected_driver","resource_class","ordinary_child_credentials","own_root_syscall_restriction",
        "Root_selected_expected_inputs","independent_final_byte_source_review","source_manifest_sha256","compiled_from_manifest_sha256"} and
        admission["schema"]=="friday.a091.external-stock-actor-admission.v1" and admission["Root_selected_expected_inputs"] is True and
        admission["independent_final_byte_source_review"] is True and admission["resource_class"]=="ordinary180_reserve10_outer1_inner4_rss256MiB" and
        admission["source_manifest_sha256"]==admission["compiled_from_manifest_sha256"]==bundle["source_manifest_sha256"],"A091_FUTURE_EXTERNAL_REVIEW_AND_TYPED_STOCK_ASSUMPTIONS")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A091_ACTUAL_INDEPENDENT_SELECTED_DRIVER")
    wanted_uid=1001 if case=="peer_uid" else 1000;wanted_gid=1001 if case=="peer_gid" else 1000
    need(admission["ordinary_child_credentials"]==[wanted_uid,wanted_gid] and type(admission["own_root_syscall_restriction"]) is bool and
        (case not in ("Root_signal_denied","Root_transport_close_denied") or admission["own_root_syscall_restriction"]),"A091_EXPLICIT_ACTUAL_OWN_STOCK_ACTOR_AND_REDUCTION")
    raw_manifest,manifest_snap=pinned(bundle["source_manifest_fd"],bundle["source_manifest_sha256"],1048576)
    manifest=json_DATA(raw_manifest)
    need(manifest["schema"]=="friday.a171.connected-whole-source-manifest.v1" and manifest["assignment"]==
        "ASTRA-E4-LAB858-SOL057-WHOLE209-ALL216-ACTUAL-SOURCE-IMPLEMENTATION-A171" and manifest["generation"]==1 and
        manifest["GO"] is False and manifest["runtime"]=="NOT_RUN" and manifest["all216"]=="REQUIRED_NOT_RUN" and
        manifest["source_execution_admission"] is False,"A118_SOURCE_MANIFEST_NOT_AUTHORITY")
    raw_oracle,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],262144);matrix=json_DATA(raw_oracle)
    need(matrix["schema"]=="friday.a091.independent-receiving-oracles.v1" and matrix["GO"] is False and matrix["runtime"]=="NOT_RUN" and
        matrix["all216"]=="SEPARATE_UNRESOLVED" and len(matrix["rows"])==len(RECEIVING_CASES) and
        {row["case"] for row in matrix["rows"]}==set(RECEIVING_CASES),"A091_COMPLETE_INDEPENDENT_PRESELECTED_ORACLE")
    rows=[row for row in matrix["rows"] if row["case"]==case];need(len(rows)==1,"A091_UNIQUE_CASE")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576);raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A091_ORIGINAL_CAPSULE_ABI")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A091_REAL_FINITE_PREREQUISITE_HEADROOM")
    need(cap[112:144]!=bytes(32) and cap[144:176].hex()==bundle["image_sha256"] and cap[176:208].hex()==expected[117] and
        cap[208:240].hex()==expected[116] and all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A091_ENTIRE_SOURCE_AND_SAME_IMAGE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A091_ACTUAL_OWN_INDEPENDENT_VIEW_AND_GROUPS")
    canonical={"schema":"friday.a091.receiving-public-input.v1","case":case,"effects":"ordinary-own-process-and-plaintext-fd-only"}
    need(raws[119]==(json.dumps(canonical,separators=(",",":"))+"\n").encode("ascii"),"A091_FULL_PINNED_ORDINARY_INPUT")
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    snapshots["manifest"]=(manifest_snap,bundle["source_manifest_fd"])
    row={**matrix["defaults"],**rows[0]};row["context"]={"session_hex":cap[112:144].hex(),"work_ns":work,"actual_uid":wanted_uid,"actual_gid":wanted_gid}
    return snapshots,work,hard,row

def whole216_producer_for(meta):
    consumer=meta["consumer"]
    if consumer.startswith("mapping_check"):return "mapping_check"
    return {"compile_bill":"compile_bill","tokens":"tokens","G1.inert_json":"inert_json",
        "disk_reservation":"disk_reservation","reservations":"reservations",
        "G1.HeaderReader.readline":"header_reader","Supervisor.bounded_fd_bytes":"bounded_fd_bytes",
        "public_admission":"public_admission","BoundedOutput.__init__":"output_count_refusal"}.get(consumer) or (
        "a171_actual" if consumer in {
        "sealed_bytes","execute_core/R4.run_wave/G1.worker","R4.drain/collect/G1.worker",
        "RetainedTree.check","R4.run_wave/G1.process_status","Run.guard","R4.close_fd",
        "R4.stop_child/collect","require_absent_target","BoundedOutput/G1.Output.check/create",
        "R4.guarded_digest","R4.guarded_write","ca_context/R4.guarded_digest",
        "ca_context/SSLContext.load_verify_locations","G1.resources",
        "G1.worker/BoundedResponse/HeaderReader","BoundedOutput/G1.Output.close",
        "execute_core/signal.signal","Run.guard/R4.stop_child","Run.guard/R4.guarded_digest",
        "terminal_bytes/Run.guard","terminal_bytes","emit_terminal","R4.run_wave/stop_child",
        "R4.run_wave/Run.guard","G1.worker/BoundedResponse/write_all","R4.collect",
        "R4.stop_child/G1.Output.close/terminal_bytes","supervise_owned/terminal_check",
        "Supervisor.seal","Supervisor.runtime_preflight","Supervisor.write_terminal",
        "Supervisor.HeldSource.bytes","Supervisor.NativeLaunchAdapter","Supervisor.supervise_owned",
        "acquisition_resources","Supervisor.cgroup_values"} else None)

def prepare_whole216(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case","source_manifest_fd","source_manifest_sha256"} and
        bundle["schema"]=="friday.a158.whole216-stock-call.v1","A158_TYPED_STOCK_CALL")
    fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A158_SAME19_FIXED_ROLES")
    admission_raw,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(admission_raw)
    need(set(admission)=={"schema","expected_driver","resource_class","ordinary_child_credentials","own_root_syscall_restriction",
        "Root_selected_expected_inputs","independent_final_byte_source_review","source_manifest_sha256","compiled_from_manifest_sha256"} and
        admission["schema"]=="friday.a091.external-stock-actor-admission.v1" and
        admission["Root_selected_expected_inputs"] is True and admission["independent_final_byte_source_review"] is True and
        admission["resource_class"]=="ordinary180_reserve10_outer1_inner4_rss256MiB" and
        admission["ordinary_child_credentials"]==[1000,1000] and admission["own_root_syscall_restriction"] is False and
        admission["source_manifest_sha256"]==admission["compiled_from_manifest_sha256"]==bundle["source_manifest_sha256"],
        "A158_INDEPENDENT_REVIEW_COMPILED_SOURCE_AND_ROOT_SELECTION")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A158_ACTUAL_EXTERNAL_STOCK_OWNER")
    manifest_raw,manifest_snap=pinned(bundle["source_manifest_fd"],bundle["source_manifest_sha256"],1048576)
    manifest=json_DATA(manifest_raw)
    need(manifest["schema"]=="friday.a171.connected-whole-source-manifest.v1" and manifest["assignment"]==
        "ASTRA-E4-LAB858-SOL057-WHOLE209-ALL216-ACTUAL-SOURCE-IMPLEMENTATION-A171" and
        manifest["generation"]==1 and manifest["source_execution_admission"] is False and manifest["runtime"]=="NOT_RUN" and
        manifest["all216"]=="REQUIRED_NOT_RUN" and manifest["GO"] is False,"A158_SOURCE_SEAL_IS_NOT_AUTHORITY")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576);raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A158_ORIGINAL_CAPSULE_WIRE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==180*10**9 and
        hard-work==10*10**9,"A158_ORIGINAL_ACTUAL_CLOCK_ENDS")
    need(cap[112:144]!=bytes(32) and cap[144:176].hex()==bundle["image_sha256"] and cap[176:208].hex()==expected[117] and
        cap[208:240].hex()==expected[116] and all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),
        "A158_WHOLE_SOURCE_IMAGE_OS_CAPSULE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A158_ACTUAL_SAME_PROTECTED_ROOT_AND_CGROUPS")
    bill=json_DATA(raws[104]);ids=list(bill["control_map"])
    need(len(ids)==216 and type(bundle["case"]) is str and bundle["case"] in ids,"A158_WHOLE_EXACT216_MEMBERSHIP")
    position=ids.index(bundle["case"]);meta=bill["control_map"][bundle["case"]]
    producer=whole216_producer_for(meta)
    need(producer is not None,"A158_REQUIRED_NOT_RUN_NO_SAFE_SOURCE_PRODUCER")
    data=json_DATA(raws[119])
    need(set(data)=={"schema","position","id","generation","producer","arguments"} and
        data["schema"]=="friday.a158.whole216-input.v1" and type(data["position"]) is int and data["position"]==position and
        data["id"]==bundle["case"] and type(data["generation"]) is int and data["generation"]==1 and
        data["producer"]==producer and type(data["arguments"]) is dict and
        raws[119]==(json.dumps(data,separators=(",",":"))+"\n").encode("ascii"),"A158_CANONICAL_ROOT_SELECTED_PER_ID_INPUT")
    a171_validate_input(data,meta,need)
    oracle_raw,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)
    row=json_DATA(oracle_raw)
    need(set(row)=={"schema","id","position","expected","mandatory_scope","SourceReady","GO"} and
        row["schema"]=="friday.a158.independent-per-id-oracle.v1" and row["id"]==bundle["case"] and
        type(row["position"]) is int and row["position"]==position and row["expected"]==meta and
        row["mandatory_scope"]=="WHOLE216_PRESERVED" and row["SourceReady"] is False and row["GO"] is False,
        "A158_INDEPENDENT_EXACT_ORIGINAL_EXPECTATION_NO_SCOPE_CUT")
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    snapshots["manifest"]=(manifest_snap,bundle["source_manifest_fd"])
    return snapshots,work,hard,{**row,"case":bundle["case"],"producer":producer,"outer_exit":0,
        "context":{"session_hex":cap[112:144].hex(),"work_ns":work}}

def oracle_whole216(outer,expected,owned,inner_stdout):
    need(outer["state"]=="OUTER_BOUNDED_DRAINED_FINISHED" and outer["reason"] is None and
        outer["terminal_completion"] is True and outer["uncertainty_sticky"] is False and
        outer["registered_workers"]==outer["reaped_workers"]==1 and outer["registry_next_sequence"]==7 and
        outer["coordinator_kernel_status_known"] is True and outer["borrowed_status_kernel_credit"] is False and
        outer["body_complete"] is False and outer["acceptance_complete"] is False,"A158_ACTUAL_ROOT_CAUSAL_COMPLETION")
    need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576 and inner_stdout.endswith(b"\n") and
        inner_stdout.count(b"\n")==1 and hashlib.sha256(inner_stdout).hexdigest()==outer["inner_terminal_sha256"],
        "A158_EXACT_ORIGINAL_COORDINATOR_RAW")
    inner=json_DATA(inner_stdout);need(inner["schema"]=="friday.a158.whole216-controller-result.v1" and
        inner["id"]==expected["id"] and inner["position"]==expected["position"] and inner["generation"]==1 and
        inner["producer_called"] is True and inner["child_created"] is True and inner["passed"] is True and
        inner["terminal_completion"] is True and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["whole216_credit"] is False and inner["F10_waiver"] is False and inner["SourceReady"] is False and inner["GO"] is False,
        "A158_ACTUAL_SCOPED_PER_ID_RESULT_NO_WHOLE_GRANT")
    control=outer["public_control"];before,after=inner["session_before"],inner["session_after"]
    need(control["origin_pid"]==owned["pid"] and control["origin_birth"]==owned["birth"] and
        before["owner"]==after["owner"]==control["coordinator_pid"] and
        before["owner_birth"]==after["owner_birth"]==control["coordinator_birth"] and
        before["origin"]==after["origin"]==owned["pid"] and before["origin_birth"]==after["origin_birth"]==owned["birth"] and
        before["next_sequence"]==2 and after["next_sequence"]==5 and before["session_ready"]==after["session_ready"]==1 and
        before["creation_poisoned"]==after["creation_poisoned"]==0,"A158_ORIGINAL_COORDINATOR_BINDING_NEVER_RESET")
    native,trace=inner["native_child"],inner["native_evidence"]
    need(native["owner"]==before["owner"] and native["origin"]==owned["pid"] and native["role"]==0 and native["pid"]>0 and
        native["pid"]!=native["owner"] and native["birth"]>0 and native["state"]==3 and native["status_known"]==1 and
        native["status"]==0 and native["wait_observed"]==native["cleanup_reaped"]==native["handle_closed"]==1 and
        native["pidfd"]==-1 and trace["stage"]==0 and trace["rights"]==trace["rights_closed"]==0 and
        trace["intent_ack_ns"]<=trace["clone_ns"]<=trace["register_send_ns"]<=trace["register_ack_ns"]<=trace["release_ns"] and
        trace["wait4_ns"]<=trace["reap_ack_ns"]<=trace["close_ns"] and trace["wait4_ns"]>trace["release_ns"],
        "A158_REAL_PRIVATE_CLONE_READY_WAIT4_ACK_CLOSE")
    streams=inner["producer_streams"];need(len(streams)==2,"A158_BOTH_PRODUCER_ORIGINAL_STREAMS")
    originals=[]
    for index,stream in enumerate(streams):
        raw=bytes.fromhex(stream["raw_hex"]);originals.append(raw)
        need(stream["stream"]==index and stream["cap"]==1048576 and stream["original_complete"] is True and
            stream["eof"] is True and stream["overflow"] is False and stream["read_error"] is None and
            stream["retention_error"] is None and stream["close_error"] is None and stream["hash_only"] is False and
            stream["raw_size"]==stream["total_seen"]==len(raw)<=1048576 and hashlib.sha256(raw).hexdigest()==stream["sha256"],
            "A158_COMPLETE_IMMUTABLE_ACTUAL_PRODUCER_RAW")
    need(not originals[1] and originals[0].endswith(b"\n") and originals[0].count(b"\n")==1,"A158_PRODUCER_FRAME_AND_ORIGINAL_STDERR")
    producer=json_DATA(originals[0]);need(producer==inner["producer"] and producer["id"]==expected["id"] and
        producer["position"]==expected["position"] and producer["producer"]==expected["producer"] and
        producer["actual_cause"]==inner["actual_cause"]==expected["expected"]["cause"] and
        producer["operation_outcome"]==inner["operation_outcome"]==("RETURNED" if producer["actual_cause"] is None else "REFUSED"),
        "A158_PERFORMED_ACTUAL_CONSUMER_OUTPUT_AND_EXPECTED_CAUSE")
    a171_validate_observation(producer["operation_observation"],expected["expected"],need,expected["id"])
    actor=producer["actual_actor"];need(actor=={"pid":native["pid"],"parent":native["owner"],"birth":native["birth"],"uid":1000,"gid":1000},
        "A158_SAME_ACTUAL_SELECTED_PERFORMING_ACTOR")
    delegated_before,delegated_after=producer["delegation_before"],producer["delegation_after"]
    for delegated in (delegated_before,delegated_after):
        need(delegated["owner"]==delegated["pid"]==native["pid"] and delegated["owner_birth"]==delegated["birth"]==native["birth"] and
            delegated["origin"]==owned["pid"] and delegated["origin_birth"]==owned["birth"] and
            delegated["uid"]==delegated["gid"]==1000 and delegated["pidfd"]==-1 and delegated["role"]==0 and
            delegated["state"]==2 and delegated["creation_poisoned"]==0 and
            all(delegated[field]==0 for field in ("wait_observed","status_known","status","cleanup_reaped","handle_closed","stop_attempted")),
            "A158_DISTINCT_CHILD_CONTEXT_NEVER_KERNEL_WAIT_AUTHORITY")
    need(delegated_before["session_ready"]==1 and delegated_before["next_sequence"]==2 and
        delegated_after["session_ready"]==0 and delegated_after["next_sequence"]==3,
        "A158_SINGLE_IMMUTABLE_CHILD_START_FINISH_LIFETIME")
    need(type(producer["operation_auxiliary_cleanup"]) is list and
        all(record["owner"]==native["pid"] and record["fd"]>=0 and record["close_attempted"] is True and
            record["closed"] is True and record["close_error"] is None and len(record["identity9"])==9 and
            all(type(value) is str and value.isdecimal() for value in record["identity9"])
            for record in producer["operation_auxiliary_cleanup"]),"A158_SAME_ACTUAL_OPERATION_FD_CUSTODY")
    for kind,phase in (("Root_selected_start",1),("Root_selected_finish",10)):
        evidence=producer[kind];need(evidence["stage"]==0 and evidence["phase"]==phase and evidence["primitive_errno"]==0 and
            evidence["observed_pid"]==evidence["expected_pid"]==owned["pid"] and
            evidence["observed_uid"]==evidence["observed_gid"]==evidence["expected_uid"]==evidence["expected_gid"]==0 and
            evidence["rights"]==evidence["rights_closed"]==0 and evidence["rights_close_errno"]==0 and
            evidence["expected"]==evidence["observed"],"A158_GENUINE_ROOT_SELECTED_CONTROLLER_EVIDENCE")
        packet=PACKET.unpack(bytes.fromhex(evidence["observed"]))
        need(packet[0]==b"FRA061P1" and packet[1].hex()==expected["context"]["session_hex"] and packet[2]==1 and
            packet[3]==phase and packet[4]==(1 if phase==1 else 2) and packet[5]==0 and packet[6]==native["pid"] and
            packet[7]==(native["owner"] if phase==1 else native["pid"]) and packet[8]==0 and
            packet[9]==expected["position"]+1 and packet[10]==native["birth"] and
            packet[11]==(before["owner_birth"] if phase==1 else native["birth"]) and packet[12]==expected["context"]["work_ns"],
            "A158_FULL96_CONTROLLER_PACKET_COMPLEMENT")
    events=outer["root_receiving"]["events"]
    selected=[e for e in events if e["kind"]==8 and e["phase"]==1]
    finished=[e for e in events if e["kind"]==1 and e["phase"]==9 and e["peer"][0]==native["pid"]]
    need(len(selected)==len(finished)==1 and selected[0]["expected_pid"]==native["pid"] and
        selected[0]["expected_parent"]==native["owner"] and selected[0]["expected_birth"]==native["birth"] and
        finished[0]["peer"]==[native["pid"],1000,1000] and finished[0]["proc_parent"]==native["owner"] and
        finished[0]["proc_birth"]==native["birth"] and selected[0]["ack_ns"]<=finished[0]["ack_ns"]<trace["wait4_ns"],
        "A158_ROOT_REGISTERED_SELECTION_FINISH_BEFORE_SAME_PARENT_PRIVATE_WAIT")
    need(0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and 0<outer["raw_self_peak_KiB"]*1024<=67108864 and
        0<=outer["outer_memory_current"]<=67108864 and 0<=outer["inner_memory_current"]<=201326592 and
        outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456 and
        producer["resources"]["aggregate_envelope"]==268435456 and producer["resources"]["coordinator_as"]==50331648 and
        producer["whole_assignment_RAM_and_implicit_IO"]=="UNKNOWN_NOT_ZERO_NOT_PROVEN","A158_ORIGINAL_ACTUAL_RESOURCE_ENVELOPE_NO_FAKE_WHOLE_RAM")
    return inner

def consume_whole216(receipt,expected,snapshots):
    inner=receipt["inner"]
    originals=tuple(bytes.fromhex(v["raw_hex"]) for v in inner["producer_streams"])
    return {"schema":"friday.a158.whole216-causal-consumption.v1","id":expected["id"],"position":expected["position"],
        "driver":receipt["driver"],"outer_origin":dict(receipt["owned"]),"coordinator":inner["session_before"],
        "performing_actor":inner["producer"]["actual_actor"],"producer":inner["producer"],
        "producer_original_stdout":originals[0],"producer_original_stderr":originals[1],
        "streams":tuple(receipt[key] for key in ("stdout_stream","stderr_stream","inner_stdout_stream","inner_stderr_stream")),
        "raw_retained":True,"native_child":inner["native_child"],"native_evidence":inner["native_evidence"],
        "bindings":receipt["bindings"],"scoped_consumer_cause":expected["expected"]["cause"],
        "all216_credit":False,"body_credit":False,"SourceReady_granted_here":False,"GO":False}

def run_whole216_positive_first(positive,controlled):
    positive_id="actual_pinned_registry_browsers_cft_positive"
    need(positive.get("schema")==controlled.get("schema")=="friday.a158.whole216-stock-call.v1" and
        positive["case"]==positive_id and controlled["case"]!=positive_id and
        positive["capsule_sha256"]!=controlled["capsule_sha256"] and
        positive["source_manifest_sha256"]==controlled["source_manifest_sha256"] and
        all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)),
        "A158_FRESH_INDEPENDENT_POSITIVE_FIRST_SAME_PROGRAM")
    a,b=(os.fstat(v["fds"][122]) for v in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A158_DISTINCT_EXTERNAL_PROTECTED_VIEWS")
    observed=run_public(positive)
    if not observed["accepted"]:return {"positive":observed,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED",
        "SourceReady":False,"GO":False}
    return {"positive":observed,"controlled":run_public(controlled),"whole216_credit":False,"SourceReady":False,"GO":False}

def receiving_packet(row,event,control,owned):
    """Full complement from fixed Source spec, actual trusted own allocations
    and independently pinned capsule. Received bytes never choose their pin.
    The sole omitted private value is the explicitly redacted unacked status.
    """
    packet=PACKET.unpack(bytes.fromhex(event["packet_hex"]))
    need(len(bytes.fromhex(event["packet_hex"]))==96,"A091_FULL96_PACKET")
    case=row["case"];context=row["context"];root_pid=owned["pid"];coord=control["coordinator_pid"];coord_birth=control["coordinator_birth"]
    need(packet[0]==b"FRA061P1" and packet[1].hex()==context["session_hex"] or
        case=="frame_session" and packet[0]==b"FRA061P1" and packet[1]==bytes((bytes.fromhex(context["session_hex"])[0]^1,))+bytes.fromhex(context["session_hex"])[1:],"A091_EXACT_FRAME_COMPLEMENT")
    need(packet[2]==(2 if case=="version" else 1) and packet[3]==row["target_phase"] and
        packet[4]==row["request_sequence"] and packet[5]==row["request_role"] and
        packet[7]==coord+(1 if case=="owner" else 0) and packet[11]==coord_birth+(1 if case=="owner_birth" else 0) and
        packet[12]==context["work_ns"]-(1 if case=="deadline" else 0),"A091_PRESELECTED_PHASE_ROLE_SEQUENCE_OWNER_DEADLINE_COMPLEMENT")
    if packet[3] in (1,2,9,12):need(packet[6]==packet[8]==packet[9]==packet[10]==0,"A091_FULL_ZERO_CONTROL_BODY")
    if packet[3]==6:need(packet[6]==packet[8]==packet[10]==0 and packet[9]==(0 if case=="abort_detail" else 24),"A091_REAL_ABORT_BODY_COMPLEMENT")
    if packet[3] in (4,7):
        if case=="register_without_intent":
            need(packet[3]==4 and row["actor_rows"]==[],"A104_REGISTER_NO_OUT_OF_INTENTION_CLONE")
            wanted_pid,wanted_birth=coord,coord_birth
        else:
            actor=row["actor_rows"][0]
            wanted_pid=coord if case in ("register_parent","reap_pid") else actor["pid"]
            wanted_birth=coord_birth if case=="register_parent" else actor["birth"]+(1 if case in ("register_birth","reap_birth") else 0)
        need(packet[6]==wanted_pid and packet[10]==wanted_birth and packet[9]==0 and
            packet[8]==0 and (packet[3]!=7 or event["status_redacted"] is True),"A091_ENTIRE_OWNED_PACKET_BODY_AND_PRIVATE_STATUS_COMPLEMENT")
    need(event["expected_peer"]==[coord,1000,1000],"A091_EXPECTED_FULL_ORIGIN")
    if case=="peer_pid":need(event["peer"][0]==row["actual_peer_pid"] and event["peer"][0]!=coord and event["peer"][1:]==[1000,1000],"A091_ACTUAL_OWN_OTHER_PEER")
    else:need(event["peer"]==[coord,context["actual_uid"],context["actual_gid"]],"A091_AUTOMATIC_ACTUAL_CREDENTIALS")
    need(event["credentials"]==1 and event["receive_bytes"]==96 and event["receive_flags"]==0 and
        event["rights"]==row["target_rights"] and event["rights_closed"]==row["target_rights_closed"] and event["rights_close_errno"]==0,"A091_EXACT_SCM_RIGHTS_RECEIPT_AND_CLOSE")
    if case=="origin_parent_argument":need(event["proc_parent"]==root_pid and event["expected_parent"]==root_pid+1 and event["proc_birth"]==event["expected_birth"]==coord_birth,"A091_GENUINE_PROC_WRONG_EXPECTED_PARENT_ARGUMENT")
    elif case=="origin_birth_argument":need(event["proc_parent"]==event["expected_parent"]==root_pid and event["proc_birth"]==coord_birth and event["expected_birth"]==coord_birth+1,"A091_GENUINE_PROC_WRONG_EXPECTED_BIRTH_ARGUMENT")
    elif case=="register_parent":need(event["handle_pid"]==packet[6]==coord and event["proc_parent"]==root_pid and event["expected_parent"]==coord and event["proc_birth"]==packet[10]==coord_birth,"A091_ACTUAL_OWN_PIDFD_WRONG_PARENT")
    elif case=="register_birth":need(event["handle_pid"]==packet[6] and event["proc_parent"]==event["expected_parent"]==coord and event["proc_birth"]+1==event["expected_birth"]==packet[10],"A091_ACTUAL_PROC_BIRTH_VS_WRONG_ARGUMENT")
    elif case=="register_stale_pidfd":need(event["handle_pid"]==-1 and packet[6]>0 and packet[10]>0,"A091_REAL_OPEN_STALE_PIDFD")
    elif case=="register_plaintext":need(event["handle_pid"]==-74,"A091_ACTUAL_PLAINTEXT_TYPE_NOT_PIDFD")
    elif case=="register_other_owned_pidfd":need(event["handle_pid"]==coord and packet[6]!=coord,"A091_OTHER_OWN_HANDLE_NOT_ADOPTED")
    elif case=="register_without_intent":
        need(event["pending_role"]==-1 and event["pending_end"]==0 and event["handle_pid"]==-1 and
            event["proc_parent"]==event["expected_parent"]==root_pid and
            event["proc_birth"]==event["expected_birth"]==coord_birth and
            event["ack_ns"]==0,"A104_REAL_PENDING_REFUSAL_BEFORE_HANDLE_OR_CHILD_ADOPTION")
    if case=="reap_status":need(event["status_argument_invalid"] is True and event["handle_pid"]==-1,"A091_REAL_INVALID_STATUS_ARGUMENT_REDACTED_NOT_ADOPTED")
    elif case=="reap_live":need(event["status_argument_invalid"] is False and event["handle_pid"]==packet[6],"A091_REAL_LIVE_EXACT_OWN_PIDFD")

def inherited_owner_oracle(inner,outer,control):
    need(inner["schema"]=="friday.a091.inherited-owner-result.v1" and inner["case"]=="inherited_start_owner" and
        inner["terminal_completion"] is True and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["GO"] is False and inner["all216"]=="SEPARATE_UNRESOLVED_NOT_RUN" and len(inner["rows"])==3,"A091_OWNER_PAIR_ONLY")
    before,after=inner["session_before"],inner["session_after"]
    need(before["owner"]==after["owner"]==control["coordinator_pid"] and before["owner_birth"]==after["owner_birth"]==control["coordinator_birth"] and
        before["session_ready"]==after["session_ready"]==1 and before["creation_poisoned"]==after["creation_poisoned"]==0 and
        before["next_sequence"]==2 and after["next_sequence"]==11,"A091_PARENT_FULL_ORDINARY_POSITIVE")
    for role,row in enumerate(inner["rows"]):
        pair,native,final,trace=row["child"],row["native"],row["final"],row["evidence"]
        need(row["role"]==pair["role"]==role and pair["pid"]==native["pid"]==final["pid"] and pair["parent"]==native["owner"]==before["owner"] and
            pair["birth"]==native["birth"]==final["birth"] and pair["copy_unchanged"] is True and pair["new_pid"]==0 and
            pair["start_result"]==pair["observe_result"]==pair["binding_result"]==pair["creation_result"]==-1 and pair["status_authority"] is False,"A091_ACTUAL_CHILD_ALL_OWNER_BINDING_REFUSALS")
        a,b=pair["inherited_before"],pair["inherited_after"]
        fixed=("owner","owner_birth","origin","origin_birth","uid","gid","next_sequence")
        need(all(a[key]==b[key] for key in fixed) and a["owner"]==before["owner"] and a["owner_birth"]==before["owner_birth"] and
            a["origin"]==before["origin"] and a["origin_birth"]==before["origin_birth"] and a["uid"]==a["gid"]==1000 and
            a["next_sequence"]==3+3*role and a["session_ready"]==b["session_ready"]==0 and a["creation_poisoned"]==0 and b["creation_poisoned"]==1,"A091_FULL_INHERITED_OWNER_BIRTH_READY_CAUSAL_PAIR")
        need(native["status_known"]==native["cleanup_reaped"]==1 and native["status"]==(23+role)<<8 and final["handle_closed"]==1 and
            final["pidfd"]==-1 and row["release_result"]==0 and trace["ready_released"]==1 and
            0<trace["intent_ack_ns"]<=trace["clone_ns"]<=trace["register_send_ns"]<=trace["register_ack_ns"]<=trace["release_ns"]<=trace["wait4_ns"]<=trace["reap_ack_ns"],"A091_ACTUAL_NATIVE_ORDINARY_WAIT_ACK_AND_CLOSE")

def oracle_receiving(outer,row,owned,inner_stdout=None):
    case=row["case"];positive=row["completion"]
    need(outer["state"]==row["outer_state"] and outer["reason"]==row["outer_cause"] and outer["terminal_completion"] is positive and
        outer["uncertainty_sticky"] is row["uncertainty"] and outer["registered_workers"]==row["root_registered"] and
        outer["reaped_workers"]==row["root_reaped"] and outer["registry_next_sequence"]==row["root_sequence"] and
        outer["coordinator_kernel_status_known"] is True and outer["borrowed_status_kernel_credit"] is False and
        outer["body_complete"] is False and outer["acceptance_complete"] is False,"A091_EXACT_FULL_ROOT_TERMINAL")
    control=outer["public_control"];root=outer["root_receiving"]
    need(control["case"]==root["case"]==case and control["origin_pid"]==owned["pid"] and control["origin_parent"]==owned["owner"] and
        control["origin_birth"]==owned["birth"] and control["coordinator_pid"]>0 and control["coordinator_birth"]>0 and
        root["schema"]=="friday.a091.actual-root-receipt.v1" and root["event_count"]==len(root["events"])<=64,"A091_ACTUAL_FIXED_ROOT_AND_COORDINATOR_IDENTITIES")
    events=root["events"];receives=[event for event in events if event["kind"]==1]
    need(all(event["kind"] in (1,2,3,4,5,6,7) and event["at_ns"]>0 for event in events),"A091_COMPLETE_TYPED_PRIMITIVE_EVENTS")
    need(root["transport_close_attempts"]==1 and root["transport_close_errno"]==(1 if case=="Root_transport_close_denied" else 0) and
        root["transport_retained"] is (case=="Root_transport_close_denied") and
        (root["transport_close_ns"]==0 if case=="Root_transport_close_denied" else root["transport_close_ns"]>0),"A091_ACTUAL_ROOT_TRANSPORT_CLOSE_RESULT")
    owners=root["owned"];need(len(owners)==1+row["root_registered"] and owners[0]["pid"]==control["coordinator_pid"] and
        owners[0]["birth"]==control["coordinator_birth"] and owners[0]["kernel_status_known"] is True and owners[0]["wait4_ns"]>0,"A091_EXACT_ROOT_DIRECT_WAIT_CUSTODY")
    for item in owners:
        need(item["close_attempts"]==1 and item["close_errno"]==0 and item["close_ns"]>0 and item["handle_retained"] is False and
            item["signal_attempts"]<=1 and (item["kernel_status_known"] or item["status"]==0),"A091_CONFIRMED_OWN_HANDLE_CLOSE_UNKNOWN_NOT_STATUS")
    need(0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and 0<outer["raw_self_peak_KiB"]*1024<=67108864 and
        0<=outer["outer_memory_current"]<=67108864 and 0<=outer["inner_memory_current"]<=201326592 and
        outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456,"A091_EXISTING_FINITE_RESOURCE_ENVELOPE")
    encoded=outer["native_inner_DATA_hex"];inner=None
    signal_case=case in ("Root_signal_positive","Root_signal_denied")
    signal_prefix=None
    if inner_stdout is None:
        need(type(encoded) is str and 0<len(encoded)<=32768,"A091_COMPLETE_CALLER_PIPE_DATA");raw=bytes.fromhex(encoded)
    else:
        need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576,"A118_COMPLETE_CALLER_PIPE_DATA");raw=inner_stdout
    need(raw.endswith(b"\n") and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A091_COMPLETE_CALLER_PIPE_SHA")
    if signal_case:
        lines=raw.splitlines(keepends=True);need(len(lines)==(1 if case=="Root_signal_positive" else 2),"A091_PRECISE_SIGNAL_PREFIX_AND_FINAL_PATHSET")
        signal_prefix=json_DATA(lines[0])
        need(signal_prefix["schema"]=="friday.a091.pre-signal-receipt.v1" and signal_prefix["case"]==case and
            signal_prefix["pid"]==control["coordinator_pid"] and signal_prefix["parent"]==owned["pid"] and signal_prefix["birth"]==control["coordinator_birth"] and
            signal_prefix["uid"]==signal_prefix["gid"]==1000 and signal_prefix["child_pid"]==owners[1]["pid"] and signal_prefix["child_birth"]==owners[1]["birth"] and
            0<signal_prefix["clone_ns"]<=signal_prefix["register_ack_ns"]<=signal_prefix["release_ns"]<=signal_prefix["ready_ns"] and
            signal_prefix["private_wait4"] is False and signal_prefix["status_known"] is False and signal_prefix["status"]==0 and
            signal_prefix["body"]=="ordinary-owned-a091\n","A091_ACTUAL_READY_WITNESS_BEFORE_ROOT_SIGNAL")
        if case=="Root_signal_denied":raw=lines[1]
    if case=="Root_signal_positive":
        need(all(item["signal_attempts"]==1 and item["signal_errno"]==0 and item["signal_ns"]>=signal_prefix["ready_ns"] and
            item["kernel_status_known"] is True and item["status"]==9 for item in owners),"A091_ACTUAL_SUCCESSFUL_ROOT_SIGNALS_AND_PRIVATE_WAIT4")
    else:
        need(type(raw) is bytes and 0<len(raw)<=1048576,"A091_COMPLETE_CALLER_RECEIPT")
        need(raw.endswith(b"\n") and raw.count(b"\n")==1,"A091_CALLER_FULL_SINGLE_FINAL_FRAME")
        inner=json_DATA(raw)
        if case=="inherited_start_owner":inherited_owner_oracle(inner,outer,control)
        else:
            need(inner["schema"]=="friday.a091.stock-caller-receipt.v1" and inner["case"]==case and inner["start_ok"] is True and
                inner["pid"]==control["coordinator_pid"] and inner["birth"]==control["coordinator_birth"] and inner["parent"]==owned["pid"] and
                inner["origin_birth"]==owned["birth"] and [inner["uid"],inner["gid"]]==[row["context"]["actual_uid"],row["context"]["actual_gid"]] and
                inner["result"]==row["caller_result"] and inner["cleanup_errno"]==0 and inner["status_authority"] is False and
                inner["plaintext_created"]==inner["plaintext_closed"]==row["plaintexts"] and len(inner["rows"])==row["created"],"A091_ACTUAL_PUBLIC_ACTOR_COMPLETE_RESULT_AND_CLEANUP")
            operand=inner["self_register_operand"]
            if case=="register_without_intent":
                need(operand=={"opened":1,"closed":1,"close_errno":0,"retained":False,
                    "pid":control["coordinator_pid"],"birth":control["coordinator_birth"],
                    "handle_pid":control["coordinator_pid"]} and inner["rows"]==[] and
                    inner["send_count"]==1 and inner["receive_count"]==0 and inner["next_sequence"]==2 and
                    inner["intent_ack_ns"]==0 and inner["last_send_type"]==4,
                    "A104_EXACT_OWN_SELF_PIDFD_CLOSED_NO_INTENT_CLONE_OR_STATUS")
            else:need(operand=={"opened":0,"closed":0,"close_errno":0,"retained":False,
                "pid":0,"birth":0,"handle_pid":-1},"A104_SELF_REGISTER_OPERAND_ABSENT_IN_OTHER_CONTOURS")
            transport=inner["transport"]
            need(transport["attempts"]==1 and transport["close_errno"]==0 and transport["closed"] is True and transport["close_ns"]>0,"A091_ACTUAL_PRODUCER_TRANSPORT_CLOSE")
            for i,item in enumerate(inner["rows"]):
                need(item["pid"]>0 and item["birth"]>0 and item["private_wait4"] is True and item["handle_closed"] is True and
                    item["close_ns"]>=item["wait4_ns"]>0 and item["registered"] is row["registered"] and item["ready"] is row["ready"] and
                    item["reap_ack"] is row["reap_ack"] and item["status_known"] is row["reap_ack"] and
                    item["status"]==((23+i)<<8 if row["reap_ack"] else 0),"A091_FULL_OWN_CHILD_CAUSAL_RECEIPT_NO_STATUS_ADOPTION")
                if item["ready"]:need(0<item["clone_ns"]<=item["register_ack_ns"]<=item["release_ns"]<=item["ready_ns"]<=item["wait4_ns"],"A091_ACTUAL_REGISTER_ACK_BEFORE_READY")
                else:need(item["register_ack_ns"]==item["release_ns"]==item["ready_ns"]==0,"A091_NO_READY_BEFORE_ACCEPTANCE")
                if item["reap_ack"]:need(item["wait4_ns"]<item["reap_ack_ns"]<=item["close_ns"],"A091_PRIVATE_WAIT4_BEFORE_ACK_AND_PUBLICATION")
            row["actor_rows"]=inner["rows"]
            resources=inner["resources"]
            need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["FSIZE"]==[2147483648,2147483648] and
                resources["CORE"]==[0,0] and resources["NOFILE"]==[512,512] and 0<resources["self_peak_bytes"]<=50331648 and
                0<=resources["children_peak_bytes"]<=201326592,"A091_ACTUAL_CALLER_AND_OWN_CHILD_RESOURCE_RECEIPT")
            ack=inner["ack_evidence"]
            need(ack["version"]==1 and ack["phase"]==row["caller_ack_phase"] and ack["stage"]==row["caller_ack_stage"] and
                ack["primitive_errno"]==row["caller_ack_errno"] and ack["expected_peer"]==[owned["pid"],0,0] and
                ack["rights"]==ack["rights_closed"]==ack["rights_close_errno"]==0,"A091_EXACT_PUBLIC_CALLER_ACK_PRIMITIVE")
            if case=="reap_invalid_ACK":
                a,b=PACKET.unpack(bytes.fromhex(ack["expected"])),PACKET.unpack(bytes.fromhex(ack["observed"]))
                wanted=list(a);wanted[3]=11
                need(a[3]==8 and tuple(wanted)==b and ack["peer"]==[owned["pid"],0,0] and ack["status_redacted"] is True,
                    "A091_FULL_INVALID_REAP_ACK_ORIGIN_AND_SINGLE_TYPE_DELTA")
            elif ack["stage"]==5:need(ack["peer"]==[-1,-1,-1] and bytes.fromhex(ack["observed"])==bytes(96),"A091_NO_INVENTED_ACK_AFTER_ROOT_REFUSAL")
            else:need(ack["peer"]==[owned["pid"],0,0] and ack["expected"]==ack["observed"],"A091_EXACT_ACTUAL_VALID_LAST_ACK")
            if signal_prefix is not None:need(inner["rows"][0]["pid"]==signal_prefix["child_pid"] and inner["rows"][0]["birth"]==signal_prefix["child_birth"],"A091_SIGNAL_PREFIX_FINAL_EXACT_BINDING")
            if case in ("abort_positive","abort_detail","abort_right"):need(inner["creation_errno"]==24,"A091_REAL_EMFILE_NOT_CREATION_LABEL")
    if case=="Root_signal_denied":need(all(item["signal_attempts"]==1 and item["signal_errno"]==1 and item["signal_ns"]>0 for item in owners),"A091_REAL_ROOT_SIGNAL_DENIAL_ONE_ATTEMPT")
    if row["target_kind"]:
        targets=[event for event in events if event["kind"] in row["target_kind"] and event["stage"]==row["stage"] and event["primitive_errno"]==row["errno_by_kind"][str(event["kind"])]]
        need(len(targets)==1,"A091_ONE_EXACT_REQUIRED_STAGE_NOT_GENERIC_FAILURE")
        target=targets[0];need(target["phase"]==row["target_phase"] or case in ("transport_closed","Root_signal_positive","Root_signal_denied","Root_transport_close_denied"),"A091_EXACT_REQUIRED_PHASE")
        if target["kind"]==1 and target["receive_bytes"]:
            if case=="peer_pid":row["actual_peer_pid"]=owners[1]["pid"]
            receiving_packet(row,target,control,owned)
        if target["kind"]==2:need(target["pending_role"]==0 and root["failure_ns"]>=target["at_ns"]>=target["pending_end"]>0 and
            inner["intent_ack_ns"]>0 and (inner["rows"]==[] if case=="pending_timeout" else
                len(inner["rows"])==1 and inner["last_send_type"]==4 and inner["last_send_ns"]>target["pending_end"]),"A091_REAL_PENDING_TIMEOUT_OR_ACTUAL_LATE_REGISTER_NOT_LABELS")
        if target["kind"]==3:need(target["receive_flags"]& (select.POLLHUP|select.POLLERR|select.POLLNVAL),"A091_ACTUAL_POLL_TRANSPORT_CLOSE")
        if target["kind"]==1 and not target["receive_bytes"]:need(target["credentials"]==target["rights"]==target["rights_closed"]==0 and target["peer"]==[-1,-1,-1],"A091_ACTUAL_EOF_NO_INVENTED_CREDENTIALS")
    else:
        phases=tuple(row["receive_phases"]) if case=="positive" else (2,6)+(2,4,7)*3+(12,9) if case=="abort_positive" else (2,4,7)*3+(12,9)
        need(tuple(event["phase"] for event in receives)==phases and all(event["stage"]==event["primitive_errno"]==0 and
            event["credentials"]==1 and event["receive_bytes"]==96 and event["rights"]==(1 if event["phase"]==4 else 0) and
            event["rights_closed"]==0 and event["rights_close_errno"]==0 and event["ack_ns"]>=event["at_ns"]>0 for event in receives),"A091_ENTIRE_ORDINARY_POSITIVE_PHASE_COMPLEMENT")
        need(all(not item["kernel_status_known"] and item["reaped"] and item["status"]==0 for item in owners[1:]),"A091_BORROWED_REAP_ACK_NEVER_ROOT_KERNEL_STATUS")
        coord,coord_birth=control["coordinator_pid"],control["coordinator_birth"]
        actors=([{**item["native"],"register_ack_ns":item["evidence"]["register_ack_ns"],"release_ns":item["evidence"]["release_ns"]} for item in inner["rows"]]
            if case=="inherited_start_owner" else inner["rows"])
        worker=0
        for offset,event in enumerate(receives):
            packet=PACKET.unpack(bytes.fromhex(event["packet_hex"]))
            need(packet[0]==b"FRA061P1" and packet[1].hex()==row["context"]["session_hex"] and packet[2]==1 and
                packet[3]==event["phase"] and packet[4]==2+offset and packet[7]==coord and packet[11]==coord_birth and
                packet[12]==row["context"]["work_ns"] and event["peer"]==event["expected_peer"]==[coord,1000,1000] and
                event["receive_flags"]==0 and event["status_argument_invalid"] is False,"A091_FULL_POSITIVE96_FRAME_SEQUENCE_ORIGIN_COMPLEMENT")
            phase=event["phase"]
            if case=="positive" and phase in (2,4,7):worker=packet[5]
            if phase in (2,6):
                need(packet[5]==worker and packet[6]==packet[8]==packet[10]==0 and packet[9]==(24 if phase==6 else 0),"A091_POSITIVE_INTENT_REAL_ABORT_BODY")
            elif phase in (4,7):
                actor=actors[worker]
                need(packet[5]==worker and packet[6]==actor["pid"] and packet[10]==actor["birth"] and packet[9]==0 and
                    packet[8]==((23+worker)<<8 if phase==7 else 0) and event["handle_pid"]==(actor["pid"] if phase==4 else -1) and
                    (phase!=4 or event["proc_parent"]==event["expected_parent"]==coord and event["proc_birth"]==event["expected_birth"]==actor["birth"]),"A091_FULL_POSITIVE_ACTUAL_OWN_HANDLE_PARENT_BIRTH_BODY")
                if phase==4:need(event["ack_ns"]<=actor["register_ack_ns"]<=actor["release_ns"],"A091_ROOT_SEND_ACK_BEFORE_ACTUAL_CALLER_RELEASE")
                else:worker+=1
            else:need(packet[5]==packet[6]==packet[8]==packet[9]==packet[10]==0,"A091_FULL_POSITIVE_DRAIN_FINISH_ZERO_BODY")
    return inner

def run_receiving_positive_first(positive,controlled):
    need(positive["schema"]==controlled["schema"]=="friday.a091.stock-public-call.v1" and positive["case"]=="positive" and
        controlled["case"]!="positive" and positive["capsule_sha256"]!=controlled["capsule_sha256"] and
        positive["source_manifest_sha256"]==controlled["source_manifest_sha256"] and positive["oracle_sha256"]==controlled["oracle_sha256"] and
        all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)),"A091_SAME_PUBLIC_PROGRAM_FULL_POSITIVE_FIRST")
    a,b=(os.fstat(value["fds"][122]) for value in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A091_FRESH_ACTUAL_INDEPENDENT_VIEWS")
    ordinary=run_connected(positive)
    if not ordinary["accepted"]:return {"positive":ordinary,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED","body_credit":False,"all216_credit":False,"SourceReady_granted_here":False,"current_GO":False,"F10_waiver":False}
    negative=run_public(controlled)
    return {"positive":ordinary,"controlled":negative,"runtime_native_controls":True,"body_credit":False,"all216_credit":False,
        "SourceReady_granted_here":False,"current_GO":False,"F10_waiver":False}

def bind_inner_streams(outer,streams):
    # Unknown until native original facts AND actual transfer correspondence
    # bind. On a refusal, retain both producer records and both immutable raws.
    for received in streams:
        received["full_original_bounded_raw"]=received["original_stream_complete"]=None
        received["native_original"]=None;received["native_original_binding_error"]=None
    transport=outer.get("native_inner_streams")
    need(type(transport) is dict and transport.get("schema")=="friday.a118.native-raw-transport.v1" and
        type(transport.get("streams")) is list and len(transport["streams"])==2,"A118_REAL_NATIVE_RAW_TRANSPORT")
    errors=[]
    for index,(producer,received) in enumerate(zip(transport["streams"],streams)):
        received["native_original"]=producer if type(producer) is dict else None
        try:
            need(type(producer) is dict,"A153_NATIVE_ORIGINAL_STREAM_RECORD")
            # A completely transferred retained prefix is still incomplete
            # when native original EOF is missing or overflow/read refusal
            # occurred. Those facts describe the original, not the transfer.
            incomplete=(producer.get("eof") is False or producer.get("overflow") is True or
                producer.get("read_errno",0)!=0 or producer.get("read_close_errno",0)!=0 or
                producer.get("total_seen")!=producer.get("retained_size"))
            if incomplete:
                received["full_original_bounded_raw"]=received["original_stream_complete"]=False
            need(producer["eof"] is True and producer["overflow"] is False and
                producer["total_seen"]==producer["retained_size"] and producer["read_errno"]==producer["read_close_errno"]==0,
                "A153_NATIVE_ORIGINAL_EOF_OVERFLOW_READ_SIZE")
            need(producer["stream"]==index and producer["cap"]==received["cap"]==STREAM_CAP and
                producer["retained_size"]==producer["emitted_bytes"]==received["retained_size"]==received["size"] and
                producer["sha256"]==received["sha256"]==received["observed_sha256"] and
                producer["prefix_hex"]==received["prefix_hex"] and
                _identity9(producer["writer_identity9"])==received["pipe_identity9"] and
                _identity9(producer["writer_identity9_after"])==received["pipe_final_identity9"] and
                producer["writer_identity9"][:7]==producer["writer_identity9_after"][:7],
                "A118_EXACT_RAW_PIPE_SIZE_SHA_IDENTITY")
            need(producer["write_errno"]==producer["close_errno"]==0 and producer["writer_closed"] is True and
                producer["close_ns"]>0 and received["transfer_eof"] is True and received["transfer_overflow"] is False and
                received["transfer_complete"] is True and received["observed_sha_complete"] is True and
                received["read_errno"] is None and received["close_errno"] is None and received["retention_error"] is None,
                "A118_INDEPENDENT_PRODUCER_AND_RECEIVER_EOF_CLOSE")
            if index==0:need(received["sha256"]==outer["inner_terminal_sha256"],"A118_NATIVE_FULL_ORIGINAL_STDOUT_SHA")
            received["full_original_bounded_raw"]=received["original_stream_complete"]=True
        except BaseException as exc:
            received["native_original_binding_error"]=public_error(exc);errors.append(public_error(exc))
    need(not errors,"A153_NATIVE_ORIGINAL_BIND_REFUSED:"+";".join(errors))

def _identity9(values):
    need(type(values) is list and len(values)==9 and all(type(v) is str and v.isdecimal() for v in values),
        "A118_IDENTITY9_DECIMAL_STRINGS")
    return values

def consume_connected_positive(receipt,row,snapshots):
    outer=receipt["outer"];stock=receipt["inner"];owned=receipt["owned"];driver=receipt["driver"]
    control=outer["public_control"];root=outer["root_receiving"]
    need(receipt["case"]==stock["case"]==root["case"]==control["case"]=="positive" and
        not receipt["causes"] and not receipt["cleanup_errors"] and stock["result"]==stock["cleanup_errno"]==0,
        "A118_POSITIVE_ACTUAL_ACCEPTED_PRODUCER")
    need(driver["pid"]==owned["owner"] and driver["pid"]!=owned["pid"] and
        control["origin_pid"]==owned["pid"] and control["origin_parent"]==driver["pid"] and
        control["origin_birth"]==owned["birth"] and owned["reaped"] is True and owned["handle_closed"] is True and
        owned["kernel_status"]==0 and 0<owned["wait_ns"]<=owned["close_ns"],"A118_DRIVER_OWNS_OUTER_NOT_COORDINATOR")
    owners=root["owned"]
    need(len(owners)==4 and owners[0]["index"]==0 and owners[0]["role"]==-1 and
        owners[0]["pid"]==control["coordinator_pid"]==stock["pid"] and
        owners[0]["birth"]==control["coordinator_birth"]==stock["birth"] and
        stock["parent"]==owned["pid"] and stock["origin_birth"]==owned["birth"] and
        owners[0]["pid"] not in (owned["pid"],driver["pid"]) and
        owners[0]["kernel_status_known"] is True and owners[0]["status"]==0,"A118_ACTUAL_ROOT_RECORDS0_DIRECT_CUSTODY")
    root_live=root["all3_live"];stock_live=stock["all3_live"]
    need(type(root_live) is dict and type(stock_live) is dict and
        root_live["inner_processes"]==stock_live["inner_processes"]==4 and
        len(root_live["members"])==len(stock_live["members"])==4 and
        0<root_live["observed_ns"]<=stock_live["observed_ns"]<row["context"]["work_ns"],"A118_TWO_ACTUAL_ALL3_LIVE_OBSERVATIONS")
    need(stock_live["uid"]==stock_live["gid"]==1000 and stock_live["same_held_image"] is True and
        root_live["same_actual_image_and_argv"] is True and
        stock_live["held_image_sha256"]==root_live["image_sha256"]==receipt["bindings"]["source_sha256"][110] and
        _identity9(stock_live["held_image_identity9"])==_identity9(root_live["image_identity9"])==
        [str(v) for v in snapshots[110][0]],"A118_SAME_ACTUAL_ROOT_SELECTED_NATIVE_IMAGE")
    argv=b"friday-approved-native-browser3\0--held-a061\0"+receipt["bindings"]["capsule_sha256"].encode("ascii")+b"\0"
    need(stock_live["own_argv_sha256"]==root_live["argv_sha256"]==hashlib.sha256(argv).hexdigest() and
        stock_live["own_argv_bytes"]==root_live["argv_bytes"]==len(argv),
        "A118_ACTUAL_INHERITED_EXACT_SELECTED_ARGV")
    seen=set();workers=[]
    for slot,(a,b,final) in enumerate(zip(root_live["members"],stock_live["members"],owners)):
        pid=final["pid"];birth=final["birth"];parent=owned["pid"] if slot==0 else stock["pid"];role=slot-1
        need(a["slot"]==b["slot"]==slot and a["role"]==b["role"]==final["role"]==role and
            a["pid"]==b["pid"]==a["handle_pid"]==b["handle_pid"]==pid and
            a["birth"]==b["birth"]==birth and a["parent"]==b["parent"]==parent and
            type(pid) is int and pid>0 and pid not in seen and pid not in (owned["pid"],driver["pid"]),
            "A118_LIVE_PHASE_PID_BIRTH_ROLE_PARENT_COMPLEMENT")
        seen.add(pid);_identity9(a["pidfd_identity9"]);_identity9(b["pidfd_identity9"])
        need(final["reaped"] is True and final["handle_retained"] is False and final["close_errno"]==0 and
            final["close_ns"]>0,"A118_ROOT_FINAL_EXACT_HANDLES")
        if slot:
            actor=stock["rows"][role]
            need(a["pidfd_identity9"]==b["pidfd_identity9"] and actor["pid"]==pid and actor["birth"]==birth and
                actor["registered"] is True and actor["ready"] is True and actor["private_wait4"] is True and
                actor["reap_ack"] is True and actor["handle_closed"] is True and actor["status_known"] is True and
                actor["status"]==(23+role)<<8 and actor["ready_ns"]<=stock_live["observed_ns"]<actor["wait4_ns"] and
                actor["wait4_ns"]<actor["reap_ack_ns"]<=actor["close_ns"] and
                final["kernel_status_known"] is False and final["status"]==0,"A118_WORKER_PRIVATE_WAIT_VERSUS_ROOT_BORROWED_ACK")
            body=bytes.fromhex(actor["body_hex"])
            need(body==b"ordinary-owned-a091\n" and actor["body_size"]==len(body),"A118_ACTUALLY_READ_WORKER_BODY")
            workers.append({"role":role,"pid":pid,"birth":birth,"parent":stock["pid"],
                "native_owner_receipt":actor,"Root_membership_receipt":final,"live_root":a,"live_private":b,
                "body_raw":body,"body_sha256":hashlib.sha256(body).hexdigest(),"Root_kernel_status_credit":False})
    receives=[event for event in root["events"] if event["kind"]==1]
    need(tuple(event["phase"] for event in receives)==tuple(row["receive_phases"])==(2,4,2,4,2,4,7,7,7,12,9),
        "A118_REGISTRY_ALLOCATE_ALL3_BEFORE_PRIVATE_REAPS")
    need(root_live["observed_ns"]<=receives[5]["ack_ns"]<=stock["rows"][2]["register_ack_ns"]<=
        stock["rows"][2]["ready_ns"]<=stock_live["observed_ns"]<min(v["wait4_ns"] for v in stock["rows"]),
        "A118_CAUSAL_ROOT_REGISTER_THEN_ALL_READY_THEN_WAIT")
    streams={key:receipt[key] for key in ("stdout_stream","stderr_stream","inner_stdout_stream","inner_stderr_stream")}
    need(all(v["raw"] is not None and v["full_original_bounded_raw"] is True for v in streams.values()),
        "A118_RETURNED_DURABLE_RAW_CUSTODY")
    return {"schema":"friday.a118.connected-positive-consumption.v1","case":"positive","status":"RECEIPTS_CHECKED",
        "driver":driver,"outer_origin":owned,"coordinator":owners[0],"workers":workers,
        "all3_live_root":root_live,"all3_live_private":stock_live,"streams":streams,"raw_retained":True,
        "session_hex":row["context"]["session_hex"],"bindings":receipt["bindings"],
        "body_credit":False,"all216_credit":False,"GO":False,"SourceReady_granted_here":False}

def run_connected(bundle):
    need(type(bundle) is dict and bundle.get("schema")=="friday.a091.stock-public-call.v1" and
        bundle.get("case")=="positive","A118_CONNECTED_ACTUAL_STOCK_INPUT")
    return run_public(bundle)

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
