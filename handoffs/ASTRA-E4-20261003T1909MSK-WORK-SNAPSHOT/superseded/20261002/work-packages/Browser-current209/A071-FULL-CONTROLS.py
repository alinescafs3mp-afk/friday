"""SOURCE ONLY. Future independently admitted owned benign full-consumer driver.

No present authority is minted here. prepare_fixture() constructs DATA ONLY;
an independent root/kernel/runtime admission must inspect and seal that data,
assemble matching fresh fixed-wire capsule pins and provide the actual protected
root/image/cgroup/source descriptors. run_held() then executes exactly fd110
through the same native public --held-a061 entry used by production. Neither
function is imported, compiled, called or tested during A061 preparation.
"""
import hashlib
import fcntl
import json
import os
import resource
import select
import signal
import stat
import struct
import tempfile
import time

SOURCES=(101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
SEALS=15
PAYLOADS=(b"owned-inert:chrome-linux64.zip",b"owned-inert:chrome-headless-shell-linux64.zip",b"owned-inert:ffmpeg-linux.zip")
MODES=("positive","http404","certificate","protocol","truncated","short_write","partial_write")
NEGATIVE={
 "http404":("THREE_BODY_RECEIPTS","HTTP_STATUS_404",False,b""),
 "certificate":("SECURITY_INTEGRITY_PROTOCOL:SSLCertVerificationError","SSLCertVerificationError",False,b""),
 "protocol":("SECURITY_INTEGRITY_PROTOCOL:TLS_EVIDENCE","TLS_EVIDENCE",False,b""),
 "truncated":("SECURITY_INTEGRITY_PROTOCOL:BODY_TRUNCATED","BODY_TRUNCATED",True,PAYLOADS[2]),
 "short_write":("SECURITY_INTEGRITY_PROTOCOL:SHORT_WRITE","SHORT_WRITE",True,b"")}
FILENAMES=("chrome-linux64.zip","chrome-headless-shell-linux64.zip","ffmpeg-linux.zip")
PATHS=tuple("archives/playwright/"+name for name in FILENAMES)
# These are real same-entry source/OS/image refusals, never operational payloads.
# Each negative bundle must be supplied by separate future fixture admission.
GUARDS={"source_pin_%d"%fd:("HELD_SOURCES",-1,fd,"pin") for fd in SOURCES}
GUARDS.update({"source_seal_%d"%fd:("HELD_SOURCES",-1,fd,"seal") for fd in SOURCES if fd!=110})
GUARDS.update({"os_"+key:("AUTHENTIC_OS_VALUES",-1,None,"independent_OS_value_delta")
               for key in ("boot","release","mount_namespace","pid_namespace","cgroup_namespace",
                           "capabilities","securebits","supplementary_groups","cgroup_identity")})
GUARDS.update({"image_"+key:("EXACT_RUNTIME_IMAGE",error,None,"independent_same_view_DATA_delta")
    for key,error in (("header",-74),("member_count",-74),("member_order",-74),
        ("complete_membership",-74),("inode",-116),("file_mode",-116),
        ("alias_target",-74),("parent_membership",-74),("cache_format",-61),
        ("cache_string",-61),("cache_resolution",-61),("ELF_format",-8),
        ("ELF_dependency",-61),("ELF_loader",-8),("loader_execute_mode",-8),
        ("import_zip",-61),("preload",-61))})

def guard_oracle(terminal,fixture,case):
    stage,error,fd,delta=GUARDS[case]
    expected=guard_primitive(case)
    require(terminal.get("state")=="REFUSED" and terminal.get("reason")==stage and
            terminal.get("primitive_errno")==error and terminal.get("primitive_stage")==expected and terminal.get("preinterpreter_rejection") is True and
            terminal.get("worker_effects")==0 and terminal.get("body_complete") is False and
            terminal.get("acceptance_complete") is False and terminal.get("terminal_completion") is False,
            "INTENDED_PUBLIC_PRIMITIVE_NO_UNRELATED_PREEMPTION")
    require(not os.path.lexists(fixture["target"]),"GUARD_TARGET_OR_BODY_EFFECT")
    measured=terminal.get("resources");kernel=terminal.get("kernel_memory")
    require(type(measured) is dict and type(kernel) is dict and
            measured.get("AS")==[67108864,67108864] and measured.get("CPU")==[180,180] and
            measured.get("FSIZE")==[2147483648,2147483648] and measured.get("NOFILE")==[512,512] and
            measured.get("CORE")==[0,0] and type(measured.get("raw_self_peak_bytes")) is int and
            0<measured["raw_self_peak_bytes"]<=67108864 and
            type(measured.get("raw_children_peak_bytes")) is int and
            0<=measured["raw_children_peak_bytes"]<=268435456 and
            type(kernel.get("outer_current")) is int and type(kernel.get("inner_current")) is int and
            0<=kernel["outer_current"]<=67108864 and 0<=kernel["inner_current"]<=201326592 and
            kernel["outer_current"]+kernel["inner_current"]<=268435456 and
            terminal.get("registered_workers")==0 and terminal.get("coordinator_created") is False and
            terminal.get("reap_status") is None,"GUARD_ACTUAL_RESOURCE_COUNT_UNKNOWN_WAIT_ORACLE")
    return {"stage":stage,"primitive_stage":expected,"errno":error,"fixed_fd":fd,"DATA_delta":delta,
            "actual_worker_effects":0,"target_absent":True,"body_hashes":[],"authority":"NOT_GRANTED"}

def generation(pid):
    fd=os.open("/proc/%d/stat"%pid,os.O_RDONLY|os.O_NOFOLLOW)
    try:raw=os.read(fd,4097)
    finally:os.close(fd)
    require(0<len(raw)<=4096 and b")" in raw,"OWNED_PROC_FORMAT")
    fields=raw.rsplit(b")",1)[1].split()
    require(len(fields)>=20,"OWNED_PROC_FIELDS")
    return int(fields[1]),int(fields[19])

def pidfd_pid(fd):
    f=os.open("/proc/self/fdinfo/%d"%fd,os.O_RDONLY|os.O_NOFOLLOW)
    try:raw=os.read(f,4097)
    finally:os.close(f)
    require(len(raw)<=4096,"OWNED_PIDFD_CAP")
    values=[int(line[4:].strip()) for line in raw.splitlines() if line.startswith(b"Pid:")]
    require(len(values)==1,"OWNED_PIDFD_TYPE")
    return values[0]

def body_bytes(target,relative):
    fd=os.open(target+"/"+relative,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==1000 and
                stat.S_IMODE(st.st_mode)==0o600 and st.st_nlink==1 and st.st_size<=4096,"BODY_CUSTODY")
        raw=os.pread(fd,4097,0)
        require(len(raw)==st.st_size,"BODY_OBSERVED_SIZE")
        return raw
    finally:os.close(fd)

def transport_oracle(terminal,fixture,case):
    """Independent constants; no observed value is selected as its expectation."""
    require(terminal.get("state")=="OUTER_BOUNDED_DRAINED_FINISHED" and
            terminal.get("registered_workers")==terminal.get("reaped_workers")==3 and
            terminal.get("terminal_completion") is True and terminal.get("uncertainty_sticky") is False,
            "PUBLIC_NATIVE_REGISTRY_CONTOUR")
    peak=terminal.get("aggregate_raw_RSS_peak_bytes")
    require(type(peak) is int and 0<peak<=268435456 and
            type(terminal.get("raw_self_peak_KiB")) is int and 0<terminal["raw_self_peak_KiB"]<=65536 and
            0<=terminal["outer_memory_current"]<=67108864 and
            0<=terminal["inner_memory_current"]<=201326592,
            "ACTUAL_RAW_RESOURCE_OBSERVATIONS")
    inner=terminal["inner_terminal"]
    require(inner["started_routes"]==inner["peak_workers"]==3 and len(inner["children"])==3 and
            all(v["lifecycle"]=="REAPED" for v in inner["children"]) and
            not inner["uncertainty_sticky"] and inner["acceptance_complete"] is False and
            inner["execution_install_root_gate_credit"] is False and inner["wire_bytes"] is None,
            "INNER_EXACT_OWNED_CONTOUR")
    require(set(inner["materials"])==set(PATHS) and len(inner["hashes"])==3 and
            set(inner["hashes"])==set(PATHS) and inner["hashes"][-1]==PATHS[2] and
            inner["stages"].get("core_admission",0)>0 and inner["stages"].get("launch",0)==3 and
            inner["stages"].get("hash_network",0)>=3 and inner["stages"].get("closed_terminal",0)>0,
            "EXACT_CALLERS_STAGES_HASH_SEQUENCE")
    observed=inner["resources"]
    require(observed["aggregate_envelope"]==268435456 and observed["outer_budget"]==67108864 and
            observed["inner_budget"]==201326592 and observed["coordinator_as"]==observed["worker_as_each"]==50331648 and
            observed["cpu_affinity"] and observed["memory_available_bytes"]>=268435456 and
            observed["var_tmp_available_bytes"]>=2147483648+67108864 and observed["ancestors"],
            "RESOURCE_CONSUMER_CAUSAL_OBSERVATIONS")
    positive=case in ("positive","partial_write")
    reason,failure,connected,third=(None,None,True,PAYLOADS[2]) if positive else NEGATIVE[case]
    if case=="http404":connected=True
    require(inner["reason"]==reason and inner["body_complete"] is positive and
            inner["state"]==("BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS" if positive else "CONTOUR_ABORTED") and
            inner["inventory"]==(fixture["target"]+"/inventory.json" if positive else None),
            "INTENDED_STATE_CAUSE_AND_INVENTORY")
    expected_bodies=PAYLOADS[:2]+(third,)
    for index,(path,wanted) in enumerate(zip(PATHS,expected_bodies)):
        record=inner["materials"][path]
        require(body_bytes(fixture["target"],path)==wanted and record["bytes"]==len(wanted) and
                record["sha256"]==hashlib.sha256(wanted).hexdigest() and
                record["observed_retained_bytes"]==len(wanted) and
                record["observed_retained_sha256"]==hashlib.sha256(wanted).hexdigest() and
                record["worker_lifecycle"]=="REAPED" and record["stop_confirmed"] is True,
                "INDEPENDENT_EXACT_BODY_HASH_BYTES")
        if index<2 or positive:
            require(record["body_complete"] is True and record["state"]=="UNACCEPTED_UNPINNED_BODY" and
                    record.get("failure") is None and record["accounting_charged_bytes"]==len(wanted),"UNCHANGED_POSITIVE_PREFIX")
        else:
            require(record["body_complete"] is False and record["state"]=="FAIL" and
                    record.get("failure")==failure and record["accounting_charged_bytes"]==16777216 and
                    inner["children"][2]["connected"] is connected,"INTENDED_THIRD_WORKER_FAILURE")
    charged=sum(len(v) for v in expected_bodies) if positive else len(PAYLOADS[0])+len(PAYLOADS[1])+16777216
    require(inner["charged_body_bytes"]==charged,"EXACT_BYTE_ACCOUNTING")
    files=[];dirs=[]
    for parent,sub,names in os.walk(fixture["target"],followlinks=False):
        dirs.append(os.path.relpath(parent,fixture["target"]))
        files.extend(os.path.relpath(parent+"/"+v,fixture["target"]) for v in names)
    expected=set(PATHS)|{"bill.json","transport-receipts.ndjson"}|({"inventory.json"} if positive else set())
    require(set(files)==expected and len(files)==(6 if positive else 5) and
            set(dirs)=={".","archives","archives/playwright"},"EXACT_FILES_DIRECTORIES_NO_EXTRA_EFFECTS")
    return inner

def require(value,cause):
    if not value:raise RuntimeError(cause)

def identity(s):
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,s.st_mode,s.st_nlink]

def write_all(fd,raw):
    view=memoryview(raw)
    while view:
        n=os.write(fd,view);require(n>0,"OWNED_FIXTURE_WRITE");view=view[n:]

def prepare_fixture(mode):
    """Real 316-file/13-directory owned retained fixture; no historical bodies.
    Must run only under a later separately admitted root-owned benign scope.
    The returned exact bytes can be independently reviewed and root sealed at119;
    no capsule/image/native/root/kernel status is inferred from their digest.
    """
    require(os.geteuid()==os.getuid()==os.getgid()==0 and mode in MODES,"FIXTURE_ADMISSION")
    parent=tempfile.mkdtemp(prefix="astra-e4-browser3-a061-offline-",dir="/var/tmp")
    os.chmod(parent,0o700);os.chown(parent,1000,1000)
    held=parent+"/held";os.mkdir(held,0o700);os.chown(held,1000,1000)
    for i in range(12):
        path=held+"/d%02d"%i;os.mkdir(path,0o700);os.chown(path,1000,1000)
    entries=[]
    for i in range(316):
        path="d%02d/f%03d"%(i%12,i);raw=("inert-%03d"%i).encode()
        fd=os.open(held+"/"+path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:write_all(fd,raw);os.fchown(fd,1000,1000);os.fsync(fd)
        finally:os.close(fd)
        entries.append([path,len(raw),hashlib.sha256(raw).hexdigest(),identity(os.stat(held+"/"+path,follow_symlinks=False)),"fixture","UNACCEPTED"])
    dirs=[]
    for key in [""]+["d%02d"%i for i in range(12)]:
        path=held+("/"+key if key else "")
        dirs.append({"path":key or ".","identity":identity(os.stat(path,follow_symlinks=False)),"membership":sorted(os.listdir(path))})
    fixture={"schema":"friday.a061.benign-fixture.v1","held_root":held,"target":parent+"/fresh-three",
      "inventory":{"root":held,"files_count":316,"entries":entries,"directories":dirs},
      "responses":[{"mode":mode if i==2 else "positive","payload":v.decode("ascii")} for i,v in enumerate(PAYLOADS)]}
    raw=json.dumps(fixture,sort_keys=True,separators=(",",":")).encode()
    require(len(raw)<=1048576,"FIXTURE_BYTE_CAP")
    return {"scope":parent,"fixture_bytes":raw,"fixture_sha256":hashlib.sha256(raw).hexdigest(),"authority":"DATA_ONLY_NOT_PROVEN"}

def check_data_fixture(raw):
    f=json.loads(raw);require(f["schema"]=="friday.a061.benign-fixture.v1","FIXTURE_SCHEMA")
    require(len(f["inventory"]["entries"])==316 and len(f["inventory"]["directories"])==13,"FIXTURE_COUNTS")
    for path,size,sha,wanted,kind,credit in f["inventory"]["entries"]:
        fd=os.open(f["held_root"]+"/"+path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            s=os.fstat(fd);require(identity(s)==wanted and s.st_uid==s.st_gid==1000 and s.st_mode&0o777==0o600 and s.st_nlink==1,"FIXTURE_IDENTITY")
            rawfile=os.pread(fd,4097,0);require(len(rawfile)==size and hashlib.sha256(rawfile).hexdigest()==sha,"FIXTURE_BYTES")
        finally:os.close(fd)
    for d in f["inventory"]["directories"]:
        path=f["held_root"]+("/"+d["path"] if d["path"]!="." else "")
        require(identity(os.stat(path,follow_symlinks=False))==d["identity"] and sorted(os.listdir(path))==d["membership"],"FIXTURE_MEMBERSHIP")
    return f

def held_inputs(fds,pin,case="positive"):
    """Validate actual supplied independently admitted immutable descriptors.
    Caller-provided flags/status labels are not accepted as evidence.
    """
    import fcntl
    require(set(fds)==set((100,111,120,121,122)+SOURCES),"EXACT_FD_SET")
    capfd=fds[100];s=os.fstat(capfd)
    require(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and s.st_size==848 and fcntl.fcntl(capfd,fcntl.F_GET_SEALS)&SEALS==SEALS,"EXTERNAL_ROOT_HELD_CAPSULE")
    cap=os.pread(capfd,849,0);require(hashlib.sha256(cap).hexdigest()==pin,"CAPSULE_PIN")
    require(cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,4,1,1000,1000,19),"BENIGN_ONLY_CAPSULE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    require(start<=time.monotonic_ns()<work<hard and hard-start==180000000000 and hard-work==10000000000,"FIXED_BENIGN_DEADLINE")
    for i,role in enumerate(SOURCES):
        fd=fds[role];s=os.fstat(fd);limit=16777216 if role==110 else 1048576
        if case in GUARDS and GUARDS[case][2]==role:
            # Only the intended bad source descriptor passes to the REAL native
            # guard. Every other immutable descriptor is checked as in positive.
            # Byte 110 is the same approved executable as the preceding positive,
            # enforced by run_pair; unknown native executable bytes are forbidden.
            require(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and 0<=s.st_size<=limit,
                    "NEGATIVE_INERT_SOURCE_DATA_TYPE")
            data=os.pread(fd,limit+1,0)
            require(len(data)==s.st_size,"NEGATIVE_EXACT_SOURCE_DATA_SIZE")
            seals=fcntl.fcntl(fd,fcntl.F_GET_SEALS)
            actual=hashlib.sha256(data).digest();wanted=cap[240+32*i:272+32*i]
            if GUARDS[case][3]=="pin":
                bad=bytearray(actual);bad[0]^=1
                require(seals&SEALS==SEALS and wanted==bytes(bad),
                        "EXACT_PIN_ONLY_DATA_DELTA_NO_SEAL_PREEMPTION")
            else:
                require(seals==7 and actual==wanted,
                        "EXACT_SEAL_ONLY_DATA_DELTA_NO_PIN_PREEMPTION")
            continue
        require(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and s.st_size<=limit and fcntl.fcntl(fd,fcntl.F_GET_SEALS)&SEALS==SEALS,"SOURCE_ROOT_KERNEL_SEALS")
        data=os.pread(fd,limit+1,0);require(len(data)==s.st_size and hashlib.sha256(data).digest()==cap[240+32*i:272+32*i],"SOURCE_EXACT_PIN")
    return cap,work,hard

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
def _run_held(fds,pin,case="positive"):
    require(case in MODES+("capsule_pin",) or case in GUARDS,"UNKNOWN_FULL_CASE")
    cap,work,hard=held_inputs(fds,pin,case)
    fixture=check_data_fixture(os.pread(fds[119],1048577,0))
    require(not os.path.lexists(fixture["target"]),"FRESH_OWNED_FIXTURE_TARGET_REQUIRED_BEFORE_EFFECTS")
    intended=case if case in MODES else "positive"
    require(fixture["responses"][2]["mode"]==intended,"INTENDED_FIXTURE_MODE")
    argpin=("0" if pin[0]!="0" else "1")+pin[1:] if case=="capsule_pin" else pin
    receipt=held_capture(fds,pin,work,hard,main_reserve_ns=500000000,argpin=argpin)
    receipt["control"]=case
    if receipt["original_error"] is not None or receipt["cleanup_errors"] or receipt["stdout_stream"] is None:
        return receipt
    # Both cap-bounded original streams and actual wait/close facts already
    # belong to receipt. Every semantic refusal returns that same receipt.
    try:
        stdout,stderr=receipt["stdout_stream"],receipt["stderr_stream"]
        require(stdout["full_original_bounded_raw"] and stderr["full_original_bounded_raw"],"FULL_ORIGINAL_RAW_CUSTODY")
        raw=stdout["raw"];require(not stderr["raw"],"UNRELATED_STDERR_PREEMPTION")
        require(raw.endswith(b"\n") and raw.count(b"\n")==1,"FULL_TERMINAL_FRAMING")
        terminal=json.loads(raw);receipt["observation"]=terminal
        require(terminal.get("acceptance_complete") is False,"NO_ADMISSION_CREDIT")
        if case=="capsule_pin":
            require(terminal.get("state")=="REFUSED" and terminal.get("reason")=="ROOT_CAPSULE" and
                terminal.get("worker_effects")==0 and terminal.get("preinterpreter_rejection") is True,
                "EXACT_CAPSULE_CAUSE_ZERO_EFFECTS")
        elif case in GUARDS:guard_oracle(terminal,fixture,case)
        else:transport_oracle(terminal,fixture,case)
        status=receipt["owned"]["wait_status"]
        require(os.WIFEXITED(status) and os.WEXITSTATUS(status)==(77 if case=="capsule_pin" or case in GUARDS else 0),"EXACT_OWNED_EXIT")
        check_data_fixture(os.pread(fds[119],1048577,0))
        receipt.update(accepted=True,passed=True,state="OBSERVED_SAME_PUBLIC_CONSUMER_NO_ADMISSION",sha256=stdout["sha256"])
    except BaseException as exc:return held_semantic_refusal(receipt,exc)
    return receipt


def run_pair(positive,negative,case):
    """Two separately admitted fresh fixtures; positive MUST finish first.
    Each tuple is (exact descriptor map, independent external capsule SHA).
    This function does not create authority or derive its own expected pin.
    """
    require(case in MODES[1:]+("capsule_pin",) or case in GUARDS,"PAIR_CASE")
    pos=_run_held(*positive,"positive")
    if pos["passed"] is not True:
        return {"case":case,"positive":pos,"negative":None,"negative_disposition":"NOT_RUN_POSITIVE_REFUSED",
            "acceptance_complete":False,"SourceReady":False,"GO":False}
    if case=="source_pin_110":
        # The executable itself remains the approved original. The negative
        # capsule's independent expected native pin is the deliberate bad DATA.
        a,b=(os.pread(bundle[0][110],16777217,0) for bundle in (positive,negative))
        require(a==b and len(a)<=16777216,"NO_UNKNOWN_NATIVE_EXECUTABLE")
    neg=_run_held(*negative,case)
    return {"case":case,"positive":pos,"negative":neg,"observation":"OBSERVED_ONLY_BY_FUTURE_INDEPENDENT_CONSUMER","acceptance_complete":False}


def run_held(fds,pin,case="positive",positive_bundle=None):
    """Public entry: an isolated negative is impossible without its actual positive."""
    if case=="positive":return _run_held(fds,pin,case)
    require(positive_bundle is not None,"FRESH_POSITIVE_BUNDLE_REQUIRED")
    return run_pair(positive_bundle,(fds,pin),case)


def guard_primitive(case):
    if case.startswith("source_pin_"):return "source_exact_pin_"+case.rsplit("_",1)[1]
    if case.startswith("source_seal_"):return "source_kernel_seals_"+case.rsplit("_",1)[1]
    if case.startswith("os_"):return case
    return {"image_header":"image_header","image_member_count":"image_header",
        "image_member_order":"image_member_metadata","image_complete_membership":"image_directory_membership",
        "image_inode":"image_member_identity","image_file_mode":"image_member_identity",
        "image_alias_target":"image_alias_closure","image_parent_membership":"image_directory_membership",
        "image_cache_format":"runtime_cache_format","image_cache_string":"runtime_cache_strings",
        "image_cache_resolution":"runtime_cache_resolution","image_ELF_format":"runtime_ELF_format",
        "image_ELF_dependency":"runtime_cache_resolution","image_ELF_loader":"runtime_ELF_loader",
        "image_loader_execute_mode":"runtime_ELF_loader","image_import_zip":"runtime_import_policy",
        "image_preload":"runtime_import_policy"}[case]


def guard_data_request(case, *, source_bytes=None, os_bytes=None, image_bytes=None,
                       runtime_data=None, runtime_modes=None, runtime_pins=None,
                       runtime_aliases=None):
    """Construct inert DATA only, without authority, FDs, paths or effects.

    A separate independently scoped admission must bind the returned delta to
    a fresh protected fixture, its exact source pins, kernel seals and capsule.
    This function cannot make that admission. The same public run_pair still
    requires a fully observed prerequisite-valid positive before a negative.
    Runtime branches return bounded, non-executable DATA changes. The producer
    applies them only after validating the unmodified approved runtime closure;
    native rejection occurs before interpreter/loader execution.
    """
    require(case in GUARDS,"UNKNOWN_GUARD_DATA_CASE")
    expected={"case":case,"authority":"DATA_ONLY_NOT_ADMITTED",
              "primitive_stage":guard_primitive(case),"primitive_errno":GUARDS[case][1],
              "state":"REFUSED","worker_effects":0,"body_hashes":[],
              "body_complete":False,"acceptance_complete":False,
              "terminal_completion":False,"independent_fresh_admission_required":True}
    if case.startswith("source_"):
        role=GUARDS[case][2];limit=16777216 if role==110 else 1048576
        require(type(source_bytes) is bytes and len(source_bytes)<=limit,"SOURCE_DATA_BOUND")
        digest=hashlib.sha256(source_bytes).digest()
        expected.update(source_fd=role,unchanged_source_sha256=digest.hex(),
                        unchanged_source_bytes=len(source_bytes))
        if case.startswith("source_pin_"):
            bad=bytearray(digest);bad[0]^=1
            expected.update(expected_pin_DATA=bytes(bad).hex(),required_source_seals=15,
                            exact_changed_fields=["one independently admitted source expected pin"])
        else:
            expected.update(expected_pin_DATA=digest.hex(),required_source_seals=7,
                            exact_changed_fields=["F_SEAL_SEAL omitted on a fresh DATA copy"])
        return expected
    if case.startswith("os_"):
        require(type(os_bytes) is bytes and len(os_bytes)==208 and
                os_bytes[:8]==b"FRA061O1" and struct.unpack_from("<4I",os_bytes,144)==(1,1000,1000,1),
                "OS_DATA_BASELINE_SCHEMA")
        offsets={"os_boot":(8,16),"os_release":(24,64),"os_mount_namespace":(88,8),
                 "os_pid_namespace":(96,8),"os_cgroup_namespace":(104,8),
                 "os_cgroup_identity":(120,8),"os_capabilities":(160,8),
                 "os_securebits":(200,4),"os_supplementary_groups":(204,4)}
        offset,width=offsets[case];changed=bytearray(os_bytes)
        if case=="os_release":
            require(b"\0" in os_bytes[24:88] and os_bytes[24]!=0,"OS_RELEASE_BASELINE")
            changed[offset]=ord("0") if changed[offset]!=ord("0") else ord("1")
        else:changed[offset]^=1
        require(changed[:offset]==os_bytes[:offset] and changed[offset+width:]==os_bytes[offset+width:],
                "OS_SINGLE_FIELD_DATA_DELTA")
        expected.update(baseline_sha256=hashlib.sha256(os_bytes).hexdigest(),
                        replacement_OS_DATA=bytes(changed),replacement_sha256=hashlib.sha256(changed).hexdigest(),
                        exact_changed_fields=[{"offset":offset,"width":width}],
                        unchanged_actual_OS_kernel_effects=True)
        return expected
    supported={"image_header","image_member_count","image_member_order","image_complete_membership",
               "image_inode","image_file_mode","image_alias_target","image_parent_membership"}
    if case not in supported:
        expected.update(runtime_guard_data(case,runtime_data,runtime_modes,
                                         runtime_pins,runtime_aliases))
        return expected
    require(type(image_bytes) is bytes and 56<=len(image_bytes)<=16777216,"IMAGE_DATA_BOUND")
    magic,version,count,total,manifest=struct.unpack_from("<8sIIQ32s",image_bytes)
    require(magic==b"FRA061I1" and version==1 and 1<count<=8192 and
            len(image_bytes)==56+count*1088 and total<=134217728,"IMAGE_DATA_BASELINE_SCHEMA")
    header=bytearray(image_bytes[:56]);rows=[bytearray(image_bytes[56+1088*i:56+1088*(i+1)]) for i in range(count)]
    if case=="image_header":struct.pack_into("<I",header,8,0)
    elif case=="image_member_count":struct.pack_into("<I",header,12,count+1)
    elif case=="image_member_order":rows[0],rows[1]=rows[1],rows[0]
    elif case in ("image_complete_membership","image_parent_membership"):
        kind=1 if case=="image_complete_membership" else 2
        selected=next((i for i,r in enumerate(rows) if struct.unpack_from("<I",r)[0]==kind and
                       bytes(r[64:576]).split(b"\0",1)[0]!=b"/"),None)
        require(selected is not None,"IMAGE_DATA_MEMBER_ANCHOR")
        row=rows.pop(selected)
        if kind==1:total-=struct.unpack_from("<Q",row,8)[0]
        struct.pack_into("<IQ",header,12,len(rows),total)
    else:
        kind=3 if case=="image_alias_target" else 1
        selected=next((i for i,r in enumerate(rows) if struct.unpack_from("<I",r)[0]==kind),None)
        require(selected is not None,"IMAGE_DATA_MEMBER_ANCHOR")
        row=rows[selected]
        if case=="image_inode":struct.pack_into("<Q",row,24,struct.unpack_from("<Q",row,24)[0]^1)
        elif case=="image_file_mode":struct.pack_into("<I",row,4,struct.unpack_from("<I",row,4)[0]^0o040)
        else:
            target=bytes(row[576:1088]).split(b"\0",1)[0]+b"-inert"
            require(len(target)<512,"IMAGE_ALIAS_DATA_BOUND");row[576:1088]=target.ljust(512,b"\0")
    changed=bytes(header)+b"".join(rows)
    require(changed!=image_bytes,"IMAGE_DATA_DELTA_REQUIRED")
    expected.update(baseline_sha256=hashlib.sha256(image_bytes).hexdigest(),
                    replacement_index_DATA=changed,replacement_sha256=hashlib.sha256(changed).hexdigest(),
                    baseline_bytes=len(image_bytes),replacement_bytes=len(changed),
                    exact_changed_fields=["one typed index cause: "+case],
                    protected_view_bytes_unchanged=True,actual_view_mutation_effects=0)
    return expected


def runtime_guard_data(case, files, modes, pins, aliases):
    """Nine isolated inert DATA causes. Never construct a runnable substitute.

    pins are independently supplied baseline expectations, not chosen from the
    bytes under test. A successful preceding same-public positive is mandatory
    at run_pair. These functions perform no file, fd, loader or kernel effects.
    """
    cases={"image_cache_format","image_cache_string","image_cache_resolution",
           "image_ELF_format","image_ELF_dependency","image_ELF_loader",
           "image_loader_execute_mode","image_import_zip","image_preload"}
    require(case in cases and type(files) is dict and type(modes) is dict and
            type(pins) is dict and type(aliases) is dict and
            set(files)==set(modes)==set(pins) and 0<len(files)<=8192,
            "RUNTIME_DATA_EXACT_BASELINE_SET")
    total=0
    for path,raw in files.items():
        require(type(path) is str and path.startswith("/") and
                all(v and v not in (".","..") for v in path.split("/")[1:]) and
                type(raw) is bytes and len(raw)<=134217728 and
                type(modes[path]) is int and 0<=modes[path]<=0o7777 and
                pins[path]=={"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()},
                "RUNTIME_DATA_EXTERNAL_BASELINE_PIN")
        total+=len(raw)
    require(total<=134217728 and "/etc/ld.so.cache" in files and
            "/usr/lib/python314.zip" not in files and "/etc/ld.so.preload" not in files,
            "RUNTIME_DATA_BASELINE_POLICY")
    cache=files["/etc/ld.so.cache"]
    require(48<=len(cache)<=1048576 and cache[:20]==b"glibc-ld.so.cache1.1",
            "RUNTIME_DATA_CACHE_BASELINE")
    count=struct.unpack_from("<I",cache,20)[0]
    start=48+24*count
    require(count<=8192 and start<=len(cache),"RUNTIME_DATA_CACHE_COUNT")
    def string(raw,offset,lower=0):
        require(lower<=offset<len(raw),"RUNTIME_DATA_STRING_OFFSET")
        end=raw.find(b"\0",offset)
        require(end>offset and end-offset<512 and
                all(33<=c<=126 for c in raw[offset:end]),"RUNTIME_DATA_STRING_BASELINE")
        return raw[offset:end],end
    cache_rows=[]
    for i in range(count):
        flags,key,value,version,hwcap=struct.unpack_from("<iIIIQ",cache,48+24*i)
        if flags&0xff00==0x300:
            name,_=string(cache,key,start);target,_=string(cache,value,start)
            cache_rows.append((name,target,key,value,48+24*i))
    def elf(raw):
        if raw[:4]!=b"\x7fELF":return None
        require(len(raw)>=64 and raw[4:7]==b"\x02\x01\x01" and
                struct.unpack_from("<HH",raw,16) in ((2,62),(3,62)),
                "RUNTIME_DATA_ELF_BASELINE")
        phoff=struct.unpack_from("<Q",raw,32)[0]
        phsize,phcount=struct.unpack_from("<HH",raw,54)
        require(phsize==56 and 0<phcount<=128 and phoff+56*phcount<=len(raw),
                "RUNTIME_DATA_ELF_HEADER_BOUND")
        headers=[struct.unpack_from("<IIQQQQQQ",raw,phoff+56*i) for i in range(phcount)]
        require(all(h[2]<=len(raw) and h[5]<=len(raw)-h[2] for h in headers),
                "RUNTIME_DATA_ELF_MEMBER_BOUND")
        interp=[h for h in headers if h[0]==3];dynamic=[h for h in headers if h[0]==2]
        require(len(interp)<=1 and len(dynamic)<=1,"RUNTIME_DATA_ELF_UNIQUE_SEGMENTS")
        loader=None
        if interp:
            off,size=interp[0][2],interp[0][5]
            require(2<=size<=512 and raw[off+size-1]==0 and
                    b"\0" not in raw[off:off+size-1] and raw[off:off+1]==b"/",
                    "RUNTIME_DATA_LOADER_BASELINE")
            loader=(off,raw[off:off+size-1].decode("ascii","strict"))
        needed=[]
        if dynamic:
            off,size=dynamic[0][2],dynamic[0][5]
            require(0<size<=65536 and size%16==0,"RUNTIME_DATA_DYNAMIC_BOUND")
            entries=[]
            for i in range(size//16):
                item=struct.unpack_from("<qQ",raw,off+16*i);entries.append(item)
                if item[0]==0:break
            require(entries[-1][0]==0 and not any(k in (15,29) for k,v in entries),
                    "RUNTIME_DATA_DYNAMIC_BASELINE")
            addresses=[v for k,v in entries if k==5];sizes=[v for k,v in entries if k==10]
            require(len(addresses)==len(sizes)==1 and 0<sizes[0]<=1048576,
                    "RUNTIME_DATA_STRTAB_BASELINE")
            places=[h[2]+addresses[0]-h[3] for h in headers if h[0]==1 and
                    h[3]<=addresses[0] and addresses[0]-h[3]<=h[5] and
                    sizes[0]<=h[5]-(addresses[0]-h[3])]
            require(len(places)==1 and places[0]+sizes[0]<=len(raw),
                    "RUNTIME_DATA_STRTAB_LOCATION")
            table=raw[places[0]:places[0]+sizes[0]]
            for tag,value in entries:
                if tag==1:
                    name,_=string(table,value)
                    require(all(c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.+-" for c in name),
                            "RUNTIME_DATA_SONAME_BASELINE")
                    needed.append((places[0]+value,name))
        return {"loader":loader,"needed":needed}
    elfs=[(path,elf(files[path])) for path in sorted(files) if files[path][:4]==b"\x7fELF"]
    require(elfs,"RUNTIME_DATA_ELF_ANCHOR")
    with_dependency=next(((p,e) for p,e in elfs if e["needed"]),None)
    with_loader=next(((p,e) for p,e in elfs if e["loader"]),None)
    result={"DATA_case":case,"primitive_stage":guard_primitive(case),
            "primitive_errno":GUARDS[case][1],"baseline_total_bytes":total,
            "baseline_pins":pins,"baseline_modes":modes,
            "baseline_aliases":aliases,"executable_payload_created":False,
            "authority":"DATA_ONLY_NOT_ADMITTED"}
    def replace(path,offset,new):
        raw=files[path];require(0<=offset and offset+len(new)<=len(raw),"RUNTIME_DATA_PATCH_BOUND")
        old=raw[offset:offset+len(new)];require(old!=new,"RUNTIME_DATA_NONEMPTY_DELTA")
        changed=raw[:offset]+new+raw[offset+len(new):]
        result.update(kind="REPLACE_FILE_DATA",path=path,offset=offset,
            old_DATA=old,new_DATA=new,replacement_DATA=changed,
            baseline_bytes=len(raw),baseline_sha256=pins[path]["sha256"],
            replacement_bytes=len(changed),replacement_sha256=hashlib.sha256(changed).hexdigest(),
            mode=modes[path],unchanged_prefix_sha256=hashlib.sha256(raw[:offset]).hexdigest(),
            unchanged_suffix_sha256=hashlib.sha256(raw[offset+len(new):]).hexdigest(),
            exact_changed_fields=[{"offset":offset,"bytes":len(new)}])
        return result
    if case=="image_cache_format":return replace("/etc/ld.so.cache",0,b"!")
    if case=="image_cache_string":
        require(cache_rows,"RUNTIME_DATA_CACHE_ACTIVE_ANCHOR")
        return replace("/etc/ld.so.cache",cache_rows[0][4]+4,b"\0"*4)
    if case=="image_cache_resolution":
        require(with_dependency is not None,"RUNTIME_DATA_DEPENDENCY_ANCHOR")
        wanted=with_dependency[1]["needed"][0][1]
        row=next((r for r in cache_rows if r[0]==wanted),None)
        require(row is not None and row[1].startswith(b"/"),"RUNTIME_DATA_CACHE_RESOLUTION_ANCHOR")
        # Printable nonabsolute DATA passes cache strings, fails resolution.
        return replace("/etc/ld.so.cache",row[3],b"!")
    if case=="image_ELF_format":return replace(elfs[0][0],4,b"\0")
    if case=="image_ELF_dependency":
        require(with_dependency is not None,"RUNTIME_DATA_DEPENDENCY_ANCHOR")
        path,e=with_dependency;offset,name=e["needed"][0];known={r[0] for r in cache_rows}
        for i in range(len(name)):
            for character in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                changed=name[:i]+bytes((character,))+name[i+1:]
                if changed!=name and changed not in known:return replace(path,offset+i,bytes((character,)))
        raise RuntimeError("RUNTIME_DATA_SINGLE_BYTE_UNUSED_SONAME_REQUIRED")
    if case=="image_ELF_loader":
        require(with_loader is not None,"RUNTIME_DATA_LOADER_ANCHOR")
        return replace(with_loader[0],with_loader[1]["loader"][0],b".")
    if case=="image_loader_execute_mode":
        require(with_loader is not None,"RUNTIME_DATA_LOADER_ANCHOR")
        logical=with_loader[1]["loader"][1];path=logical
        for _ in range(8):
            candidates=[p for p in aliases if path==p or path.startswith(p+"/")]
            if not candidates:break
            prefix=max(candidates,key=len);target=aliases[prefix]
            require(type(target) is str and 0<len(target)<512 and "\0" not in target,
                    "RUNTIME_DATA_ALIAS_BASELINE")
            path=os.path.normpath(os.path.join(os.path.dirname(prefix),target))+path[len(prefix):]
        require(path in files and modes[path]&0o111,"RUNTIME_DATA_EXECUTABLE_LOADER_BASELINE")
        result.update(kind="REMOVE_LOADER_EXECUTE_BITS",path=path,
            baseline_sha256=pins[path]["sha256"],baseline_bytes=len(files[path]),
            baseline_mode=modes[path],mode=modes[path]&~0o111,
            replacement_bytes=len(files[path]),replacement_sha256=pins[path]["sha256"],
            replacement_DATA=files[path],exact_changed_fields=["loader execute bits"])
        return result
    path="/usr/lib/python314.zip" if case=="image_import_zip" else "/etc/ld.so.preload"
    require(path not in files,"RUNTIME_DATA_FORBIDDEN_MEMBER_ALREADY_PRESENT")
    result.update(kind="ADD_EMPTY_FORBIDDEN_DATA_MEMBER",path=path,replacement_DATA=b"",
        replacement_bytes=0,replacement_sha256=hashlib.sha256(b"").hexdigest(),mode=0o444,
        exact_changed_fields=["one fixed zero-byte nonexecutable DATA member"])
    return result


def freeze_guard_DATA(raw,seals=15):
    """Future root-scoped inert sealer, never a capsule/approval writer."""
    require(os.getuid()==os.geteuid()==os.getgid()==0 and type(raw) is bytes and
            len(raw)<=16777216 and seals in (7,15),"GUARD_DATA_SEAL_SCOPE")
    fd=os.memfd_create("friday-a071-inert-guard-data",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd,0o400);write_all(fd,raw);fcntl.fcntl(fd,fcntl.F_ADD_SEALS,seals)
        require(os.pread(fd,len(raw)+1,0)==raw and fcntl.fcntl(fd,fcntl.F_GET_SEALS)==seals,
                "GUARD_DATA_EXACT_NEW_SEALED_DESCRIPTOR")
        return fd
    except BaseException:os.close(fd);raise


class FreshGuardAssembly:
    """Connected fresh fixture -> protected producer -> real sealed fds ->
    independently supplied capsule -> the SAME native --held-a061 consumer.

    Construction is Source, not present authority. Each instance requires an
    actual PreparedView and independent baseline source/OS pins. No capsule,
    OS-value writer, cgroup provisioner or privileged fixture service exists here.
    Neither bare requests nor an assembly count constitute a passed control.
    """
    def __init__(self,case,view,source_fds,source_pins,outer_group,inner_group):
        require(case=="positive" or case in GUARDS,"GUARD_ASSEMBLY_CASE")
        require(os.getuid()==os.geteuid()==os.getgid()==0 and
                set(source_fds)==set(SOURCES)-{119} and
                set(source_pins)==set(SOURCES)-{119},"GUARD_EXTERNAL_BASELINE_SOURCE_SET")
        require(view.ready and view.rootfd is not None and view.indexfd is not None,
                "GUARD_ACTUAL_PREPARED_VIEW")
        expected_view_case=case if case.startswith("image_") else None
        require(view.control_case==expected_view_case,"GUARD_VIEW_SINGLE_TYPED_CAUSE")
        self.case,self.view=case,view;self.owned_fds=[];self.bundle=None;self.closed=False
        self.fds={111:view.indexfd,122:view.rootfd,120:outer_group,121:inner_group,**source_fds}
        self.actual_source={};self.expected_source={}
        try:
            # The original runtime manifest was independently pinned before any
            # allowed DATA overlay. Ordinary program/native bytes stay approved.
            require(source_pins[117]==view.original_manifest_sha,"GUARD_BASELINE_MANIFEST_PIN")
            for role,fd in source_fds.items():
                limit=16777216 if role==110 else 1048576;s=os.fstat(fd)
                require(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and
                        0<=s.st_size<=limit and fcntl.fcntl(fd,fcntl.F_GET_SEALS)==15,
                        "GUARD_BASELINE_HELD_CUSTODY")
                raw=os.pread(fd,limit+1,0)
                require(len(raw)==s.st_size and hashlib.sha256(raw).hexdigest()==source_pins[role],
                        "GUARD_BASELINE_EXTERNAL_PIN")
                self.actual_source[role]=raw
            fixture=prepare_fixture("positive")
            self.fixture_scope=fixture["scope"];self.fixture_bytes=fixture["fixture_bytes"]
            self.actual_source[119]=self.fixture_bytes
            # A newly built view may have a changed DATA manifest. This is an
            # observation for independent capsule approval, not an expected Root
            # context selected from a failed run or granted by this producer.
            self.actual_source[117]=view.raw_manifest
            if case.startswith("os_"):
                self.DATA=guard_data_request(case,os_bytes=self.actual_source[116])
                self.actual_source[116]=self.DATA["replacement_OS_DATA"]
            elif case.startswith("source_"):
                self.DATA=guard_data_request(case,source_bytes=self.actual_source[GUARDS[case][2]])
            elif case.startswith("image_"):self.DATA=view.control_delta
            else:self.DATA=None
            for role in (117,119)+( (116,) if case.startswith("os_") else () ):
                fd=freeze_guard_DATA(self.actual_source[role]);self.owned_fds.append(fd);self.fds[role]=fd
            for role,raw in self.actual_source.items():self.expected_source[role]=hashlib.sha256(raw).digest()
            if case.startswith("source_pin_"):
                role=GUARDS[case][2];self.expected_source[role]=bytes.fromhex(self.DATA["expected_pin_DATA"])
            elif case.startswith("source_seal_"):
                role=GUARDS[case][2];fd=freeze_guard_DATA(self.actual_source[role],7)
                self.owned_fds.append(fd);self.fds[role]=fd
            self.view_before=view.check_protected_members()
            self.source_before=self.source_observations()
            self.fixture=check_data_fixture(self.fixture_bytes)
            require(not os.path.lexists(self.fixture["target"]),"GUARD_FRESH_TARGET_BEFORE_ADMISSION")
        except BaseException:self.close();raise

    def source_observations(self):
        result={}
        for role in SOURCES:
            fd=self.fds[role];s=os.fstat(fd);raw=os.pread(fd,16777217 if role==110 else 1048577,0)
            require(len(raw)==s.st_size and raw==self.actual_source[role],"GUARD_SOURCE_BYTES_OR_DRIFT")
            seals=fcntl.fcntl(fd,fcntl.F_GET_SEALS)
            expected=7 if self.case=="source_seal_%d"%role else 15
            require(seals==expected,"GUARD_EXACT_SEAL_ISOLATION")
            result[role]={"identity":identity(s),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"seals":seals}
        return result

    def admit(self,capsule_fd,independent_capsule_sha):
        """Consume independently fixed Root context; no approval is inferred from
        fixture data, producer flags, matching hashes, or previous observations.
        The supplied pin must be from separate Root review of this exact assembly.
        """
        require(not self.closed and self.bundle is None,"GUARD_SINGLE_ADMISSION")
        self.fds[100]=capsule_fd
        cap,work,hard=held_inputs(self.fds,independent_capsule_sha,self.case)
        root=os.fstat(self.fds[122]);outer=os.fstat(self.fds[120]);inner=os.fstat(self.fds[121])
        require(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
                struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,
                    os.stat("/proc/self/ns/mnt").st_ino,outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),
                "GUARD_INDEPENDENT_CAPSULE_ACTUAL_CONTEXT")
        require(cap[112:144]!=b"\0"*32 and cap[144:176].hex()==self.view.image_sha and
                cap[176:208].hex()==hashlib.sha256(self.actual_source[117]).hexdigest() and
                cap[208:240].hex()==hashlib.sha256(self.actual_source[116]).hexdigest() and
                all(cap[240+32*i:272+32*i]==self.expected_source[role] for i,role in enumerate(SOURCES)),
                "GUARD_CAPSULE_FULL_DATA_CORRESPONDENCE")
        require(self.source_observations()==self.source_before and
                self.view.check_protected_members()==self.view_before,"GUARD_PRE_ADMISSION_COMPLEMENT")
        self.bundle=(dict(self.fds),independent_capsule_sha);self.work,self.hard=work,hard
        return self.bundle

    def after(self):
        require(self.source_observations()==self.source_before and
                self.view.check_protected_members()==self.view_before,
                "GUARD_EXACT_BYTE_HASH_MODE_INODE_COMPLEMENT_AFTER_CONSUMER")
        check_data_fixture(self.fixture_bytes)
        if self.case!="positive":require(not os.path.lexists(self.fixture["target"]),"GUARD_ZERO_OUTPUT_EFFECT")
        return {"source_descriptor_count":19,"protected_members":len(self.view_before),
            "source_bytes_hash_seals_unchanged":True,"protected_view_bytes_metadata_unchanged":True,
            "retained_files":316,"retained_directories":13,"target_absent":not os.path.lexists(self.fixture["target"]),
            "authority":"NOT_GRANTED"}

    def close(self):
        if getattr(self,"closed",False):return
        self.closed=True;failure=None
        for fd in getattr(self,"owned_fds",[]):
            try:os.close(fd)
            except OSError as exc:failure=exc
        self.owned_fds=[]
        if failure is not None:raise RuntimeError("GUARD_OWNED_DESCRIPTOR_CLOSE_UNCONFIRMED") from failure


def run_fresh_guard_pair(positive,negative,case):
    """Every one of63 has its OWN complete fresh positive first, then its one
    isolated typed DATA negative through real public native110. Actual waits,
    raw resources, exact bytes/hashes/counts/state/STAGE are required by run_pair.
    No row is passed when prerequisites, custody, cleanup or its cause differ.
    """
    require(case in GUARDS and positive.case=="positive" and negative.case==case and
            positive.bundle is not None and negative.bundle is not None and
            positive.fixture_scope!=negative.fixture_scope and
            (os.fstat(positive.view.rootfd).st_dev,os.fstat(positive.view.rootfd).st_ino)!=
            (os.fstat(negative.view.rootfd).st_dev,os.fstat(negative.view.rootfd).st_ino),
            "GUARD_INDEPENDENT_FRESH_PUBLIC_PAIR")
    require(positive.actual_source[110]==negative.actual_source[110],"GUARD_SAME_APPROVED_NATIVE_BYTES")
    result=run_pair(positive.bundle,negative.bundle,case)
    result["positive_complement"]=positive.after();result["negative_complement"]=negative.after()
    result["DATA_cause"]={"case":case,"primitive_stage":guard_primitive(case),
        "primitive_errno":GUARDS[case][1],"construction":guard_DATA_receipt(negative.DATA),
        "independent_capsule_pins":[positive.bundle[1],negative.bundle[1]]}
    result["coverage_credit"]="FUTURE_OBSERVATION_REQUIRES_INDEPENDENT_REVIEW"
    return result


def guard_DATA_receipt(delta):
    """Bounded exact cause/complement evidence without emitting raw DATA bytes."""
    require(type(delta) is dict,"GUARD_DATA_RECEIPT_TYPE")
    fields=("case","DATA_case","primitive_stage","primitive_errno","kind","path","offset",
        "baseline_sha256","baseline_bytes","replacement_sha256","replacement_bytes","baseline_mode","mode",
        "unchanged_prefix_sha256","unchanged_suffix_sha256","exact_changed_fields","source_fd",
        "unchanged_source_sha256","unchanged_source_bytes","expected_pin_DATA","required_source_seals",
        "protected_view_bytes_unchanged","actual_view_mutation_effects","executable_payload_created")
    result={key:delta[key] for key in fields if key in delta}
    for key in ("old_DATA","new_DATA"):
        if key in delta:
            require(type(delta[key]) is bytes and len(delta[key])<=8,"GUARD_DATA_PATCH_RECEIPT_BOUND")
            result[key+"_hex"]=delta[key].hex()
    if "baseline_pins" in delta:
        raw=json.dumps(delta["baseline_pins"],sort_keys=True,separators=(",",":")).encode()
        result["external_baseline_pin_set_sha256"]=hashlib.sha256(raw).hexdigest()
        result["external_baseline_file_count"]=len(delta["baseline_pins"])
    result["authority"]="NOT_GRANTED"
    require(len(json.dumps(result,sort_keys=True,separators=(",",":")).encode())<=8192,"GUARD_DATA_RECEIPT_CAP")
    return result


def run_all63_fresh_guards(pairs,independent_hard_ns):
    """Finite exact-suite caller. Root supplies each independently admitted
    pair as it becomes ready; no capsule service/callback or approval is created.
    This same process executes pairs sequentially, with a60-second final reserve,
    preserving the global four-process/native64+192MiB resource contract.
    All63 real public positives and their negatives must finish. No planned,
    skipped, helper-only or prerequisite-preempted row receives coverage credit.
    """
    now=time.monotonic_ns()
    require(type(independent_hard_ns) is int and now+60*10**9<independent_hard_ns<=now+1200*10**9,
            "GUARD_SUITE_INDEPENDENT_1200_60_BOUND")
    seen=set();scopes=set();pins=set();rows=[]
    for case,positive,negative in pairs:
        require(time.monotonic_ns()<independent_hard_ns-60*10**9 and
                case in GUARDS and case not in seen,"GUARD_SUITE_CASE_OR_DEADLINE")
        require(positive.bundle is not None and negative.bundle is not None and
                positive.fixture_scope not in scopes and negative.fixture_scope not in scopes and
                positive.bundle[1] not in pins and negative.bundle[1] not in pins,
                "GUARD_SUITE_INDEPENDENT_FRESH_PAIR_INPUTS")
        require(max(positive.hard,negative.hard)<=independent_hard_ns-60*10**9,
                "GUARD_SUITE_EACH_OWNED_LIFETIME_WITHIN_GLOBAL_RESERVE")
        scopes.update((positive.fixture_scope,negative.fixture_scope));pins.update((positive.bundle[1],negative.bundle[1]))
        try:
            observed=run_fresh_guard_pair(positive,negative,case)
            pos=observed["positive"];neg=observed["negative"]
            require(pos["owned"]["reaped"] and neg["owned"]["reaped"] and
                    not pos["owned"]["uncertainty_sticky"] and not neg["owned"]["uncertainty_sticky"],
                    "GUARD_SUITE_ALL_OWNED_NATIVE_CHILDREN_CLOSED")
            rows.append({"case":case,"positive_terminal_sha256":pos["sha256"],
                "negative_terminal_sha256":neg["sha256"],"positive_state":pos["observation"]["state"],
                "negative_state":neg["observation"]["state"],"primitive_stage":guard_primitive(case),
                "primitive_errno":GUARDS[case][1],"positive_registered_reaped_workers":[3,3],
                "negative_registered_workers":neg["observation"]["registered_workers"],
                "negative_resources":neg["observation"]["resources"],
                "negative_kernel_memory":neg["observation"]["kernel_memory"],
                "positive_complement":observed["positive_complement"],
                "negative_complement":observed["negative_complement"],
                "DATA_cause":observed["DATA_cause"]})
            seen.add(case)
        finally:
            failure=None
            for assembly in (negative,positive):
                try:assembly.close()
                except BaseException as exc:failure=exc
            if failure is not None:raise RuntimeError("GUARD_SUITE_CLOSE_UNCONFIRMED") from failure
    require(seen==set(GUARDS) and len(rows)==63 and len(scopes)==len(pins)==126 and
            time.monotonic_ns()<independent_hard_ns,"GUARD_SUITE_EXACT_ALL63_OBSERVED_ON_TIME")
    result={"schema":"friday.a071.observed-fresh-guard-suite.v1","controls":rows,
        "complete_public_positives":63,"complete_isolated_DATA_negatives":63,
        "owned_top_native_children_reaped":126,"network_effects":0,
        "acceptance_complete":False,"source_or_runtime_GO":False,
        "independent_review_required":True}
    require(len(json.dumps(result,sort_keys=True,separators=(",",":")).encode())<=1048576,
            "GUARD_SUITE_REAL_RECEIPT_OUTPUT_CAP")
    return result
