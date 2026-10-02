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
            require(stat.S_ISREG(s.st_mode) and s.st_size<=limit,"NEGATIVE_INERT_SOURCE_DATA_TYPE")
            continue
        require(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and s.st_size<=limit and fcntl.fcntl(fd,fcntl.F_GET_SEALS)&SEALS==SEALS,"SOURCE_ROOT_KERNEL_SEALS")
        data=os.pread(fd,limit+1,0);require(len(data)==s.st_size and hashlib.sha256(data).digest()==cap[240+32*i:272+32*i],"SOURCE_EXACT_PIN")
    return cap,work,hard

def _run_held(fds,pin,case="positive"):
    """Actual owned fork -> fixed-fd static native entry -> full consumer.
    No subprocess library, path execution, shell, GET or image construction.
    Attachment names ONLY the actual owned child itself before native exec.
    An unknown PID or lost ownership remains STOP_UNCONFIRMED, never signalled.
    """
    require(case in MODES+("capsule_pin",) or case in GUARDS,"UNKNOWN_FULL_CASE")
    cap,work,hard=held_inputs(fds,pin,case)
    fixture=check_data_fixture(os.pread(fds[119],1048577,0))
    intended=case if case in MODES else "positive"
    require(fixture["responses"][2]["mode"]==intended,"INTENDED_FIXTURE_MODE")
    out=os.pipe2(os.O_CLOEXEC);err=os.pipe2(os.O_CLOEXEC);barrier=os.pipe2(os.O_CLOEXEC)
    record={"pid":None,"pidfd":None,"birth":None,"owner":os.getpid(),"reaped":False,"wait_status":None,"stop_attempted":False,"uncertainty_sticky":False}
    pid=os.fork()
    if pid==0:
        os.close(out[0]);os.close(err[0]);os.close(barrier[1])
        released=False
        while time.monotonic_ns()<work:
            if select.select([barrier[0]],[],[],.01)[0]:
                released=os.read(barrier[0],1)==b"A";break
        if not released:os._exit(124)
        os.close(barrier[0])
        # Relocate EVERY source, including both output pipes, before ANY dup2.
        # The released and closed barrier has no later numeric-fd authority.
        mappings={**fds,1:out[1],2:err[1]}
        relocated={dst:fcntl.fcntl(src,fcntl.F_DUPFD_CLOEXEC,400) for dst,src in mappings.items()}
        for dst,src in relocated.items():os.dup2(src,dst,inheritable=True)
        for src in relocated.values():os.close(src)
        attach=os.open("cgroup.procs",os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=120)
        try:write_all(attach,str(os.getpid()).encode())
        finally:os.close(attach)
        keep=set(fds)|{1,2}
        for name in os.listdir("/proc/self/fd"):
            fd=int(name)
            if fd not in keep:
                try:os.close(fd)
                except OSError:pass
        argpin=("0" if pin[0]!="0" else "1")+pin[1:] if case=="capsule_pin" else pin
        os.execve(110,["friday-approved-native-browser3","--held-a061",argpin],{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"})
        os._exit(125)
    # Durable actual direct intention exists before the optional owner handle.
    record["pid"]=pid;os.close(out[1]);os.close(err[1]);os.close(barrier[0])
    buffers=[bytearray(),bytearray()];pipes={out[0]:0,err[0]:1}
    try:
        record["pidfd"]=os.pidfd_open(pid,0)
        owner,birth=generation(pid)
        require(owner==record["owner"] and pidfd_pid(record["pidfd"])==pid and generation(pid)==(owner,birth),"ACTUAL_OWNED_START_PIDFD_GENERATION")
        record["birth"]=birth
        write_all(barrier[1],b"A");os.close(barrier[1]);barrier=(barrier[0],-1)
        for fd in pipes:os.set_blocking(fd,False)
        while pipes or not record["reaped"]:
            require(time.monotonic_ns()<hard-500000000,"FULL_CONTROL_DEADLINE_WITH_CLEANUP_RESERVE")
            ready,_,_=select.select(list(pipes),[],[],.02)
            for fd in ready:
                data=os.read(fd,65536)
                if not data:os.close(fd);del pipes[fd];continue
                buffers[pipes[fd]].extend(data);require(len(buffers[pipes[fd]])<=1048576,"FULL_OUTPUT_CAP")
            if not record["reaped"]:
                actual,status=os.waitpid(pid,os.WNOHANG)
                if actual==pid:record.update(reaped=True,wait_status=status)
        require(not buffers[1],"UNRELATED_STDERR_PREEMPTION")
        raw=bytes(buffers[0]);require(raw.endswith(b"\n") and raw.count(b"\n")==1,"FULL_TERMINAL_FRAMING")
        terminal=json.loads(raw);require(terminal.get("acceptance_complete") is False,"NO_ADMISSION_CREDIT")
        if case=="capsule_pin":
            require(terminal.get("state")=="REFUSED" and terminal.get("reason")=="ROOT_CAPSULE" and terminal.get("worker_effects")==0 and terminal.get("preinterpreter_rejection") is True,"EXACT_CAPSULE_CAUSE_ZERO_EFFECTS")
        elif case in GUARDS:
            guard_oracle(terminal,fixture,case)
        else:
            transport_oracle(terminal,fixture,case)
        require(os.WIFEXITED(record["wait_status"]) and os.WEXITSTATUS(record["wait_status"])==(77 if case=="capsule_pin" or case in GUARDS else 0),"EXACT_OWNED_EXIT")
        check_data_fixture(os.pread(fds[119],1048577,0))
        return {"control":case,"state":"OBSERVED_SAME_PUBLIC_CONSUMER_NO_ADMISSION","passed":True,"observation":terminal,"owned":record,"sha256":hashlib.sha256(raw).hexdigest()}
    finally:
        if not record["reaped"]:
            record["stop_attempted"]=True
            # Direct unreaped parent ownership is preserved; no foreign handle.
            try:
                actual,status=os.waitpid(pid,os.WNOHANG)
                if actual==pid:record.update(reaped=True,wait_status=status)
                else:
                    require(actual==0,"DIRECT_WAIT_OWNERSHIP")
                    if record["pidfd"] is not None:
                        require(pidfd_pid(record["pidfd"])==pid and (record["birth"] is None or generation(pid)==(record["owner"],record["birth"])),"OWNED_HANDLE_BEFORE_SIGNAL")
                        signal.pidfd_send_signal(record["pidfd"],signal.SIGKILL)
                    else:os.kill(pid,signal.SIGKILL)
                end=min(time.monotonic_ns()+1000000000,hard)
                while time.monotonic_ns()<end:
                    actual,status=os.waitpid(pid,os.WNOHANG)
                    if actual==pid:record.update(reaped=True,wait_status=status);break
                    select.select([],[],[],.005)
                if not record["reaped"]:record["uncertainty_sticky"]=True
            except BaseException:record["uncertainty_sticky"]=True
        for fd in list(pipes)+([record["pidfd"]] if record["pidfd"] is not None else [])+([barrier[1]] if barrier[1]>=0 else []):
            try:os.close(fd)
            except OSError:record["uncertainty_sticky"]=True
        if record["uncertainty_sticky"]:raise RuntimeError("STOP_UNCONFIRMED")

def run_pair(positive,negative,case):
    """Two separately admitted fresh fixtures; positive MUST finish first.
    Each tuple is (exact descriptor map, independent external capsule SHA).
    This function does not create authority or derive its own expected pin.
    """
    require(case in MODES[1:]+("capsule_pin",) or case in GUARDS,"PAIR_CASE")
    pos=_run_held(*positive,"positive")
    require(pos["passed"] is True,"POSITIVE_PRECONDITION_NOT_OBSERVED")
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
