"""A061 connected held-byte/runtime/authority/registry consumers. SOURCE ONLY.
Native code owns pre-interpreter effects. These checks add no retroactive proof.
All future calls require separately reviewed native binaries and root admission.
"""
import ctypes
import fcntl
import hashlib
import json
import os
import re
import resource
import select
import signal
import socket
import stat
import struct
import sys
import time
import types

BASE = "/var/tmp/friday-astra-browser-native-authoritative-caller-a079-g1/A071"
SOURCE_FDS = (101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
SOURCE_ROLES = ("executor","controls","supervisor","bill","G1","R4","owner","inventory","CA",
                "native","worker","bridge","producer","schema","os","manifest","contract","fixture","owned_consumer")
CAP = struct.Struct("<8s6I10Q32s32s32s32s" + "32s" * 19)
IMAGE = struct.Struct("<8sIIQ32s")
MEMBER = struct.Struct("<IIQQQ32s512s512s")
PACKET = struct.Struct("<8s32sIIIIiiiiQQQ")
SEALS = fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
ENV = {"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}
MODES = {1:"--controls",2:"--preflight",3:"--execute",4:"--benign"}
BRIDGE = "/usr/lib/x86_64-linux-gnu/libfriday-a061-owned.so"
ADMISSION = None
NATIVE = None
OBSERVATIONS = []

class Refused(Exception):
    pass

def need(ok, reason):
    if not ok: raise Refused(reason)

def held(fd, sha, cap, *, root=True):
    need(type(sha) is str and re.fullmatch("[0-9a-f]{64}",sha),"A061_PIN")
    st=os.fstat(fd)
    need(stat.S_ISREG(st.st_mode) and 0<=st.st_size<=cap,"A061_HELD_SIZE")
    need(not root or st.st_uid==st.st_gid==0,"A061_ROOT_AUTHORITY")
    need(fcntl.fcntl(fd,fcntl.F_GET_SEALS)&SEALS==SEALS,"A061_KERNEL_SEALS")
    h=hashlib.sha256();parts=[];off=0
    while off<st.st_size:
        part=os.pread(fd,min(65536,st.st_size-off),off)
        need(bool(part),"A061_HELD_READ");off+=len(part);h.update(part);parts.append(part)
    need(h.hexdigest()==sha and os.fstat(fd)==st,"A061_HELD_SHA_OR_DRIFT")
    return b"".join(parts)

def decode_capsule(raw,sha, *, now_ns=None):
    """The same typed byte consumer is used by production and structural controls.
    Parsing owned fixture bytes never confers root or kernel authority.
    """
    need(type(raw) is bytes and len(raw)==CAP.size,"CAP_SIZE")
    need(hashlib.sha256(raw).hexdigest()==sha,"CAP_SHA")
    v=CAP.unpack(raw)
    need(v[0]==b"FRA061C1" and v[1]==1 and v[3]==1 and v[6]==19,"CAP_IDENTITY")
    need(v[2] in MODES and v[4:6]==(1000,1000),"CAP_ROLE_CREDENTIALS")
    start,work,hard=v[7:10];now=time.monotonic_ns() if now_ns is None else now_ns
    wall,reserve=(180,10) if v[2] in (1,4) else (1200,60)
    need(0<=start<=now<work<hard and hard-start==wall*10**9 and hard-work==reserve*10**9,"CAP_DEADLINE")
    need(all(x>0 for x in (v[10],v[11],v[12],v[13],v[14],v[15],v[16])) and
         v[17]!=b"\0"*32 and all(x!=b"\0"*32 for x in v[18:]),"CAP_COMPLETE_VALUES")
    pins=dict(zip(SOURCE_ROLES,(x.hex() for x in v[21:])))
    need(pins["os"]==v[20].hex() and pins["manifest"]==v[19].hex(),"CAP_AUTHORITY_PIN_CORRESPONDENCE")
    return {"schema":"friday.browser3.native-held.v2","assignment":"ASTRA-E4-BROWSER-FRESH-FIXTURE-AND-OWNED-CALLER-CLOSURE-A071",
      "generation":1,"mode":MODES[v[2]],"mode_id":v[2],"uid":v[4],"gid":v[5],
      "start_ns":start,"work_ns":work,"hard_ns":hard,"started":start/1e9,"work":work/1e9,"hard":hard/1e9,
      "root_identity":v[10:12],"mount_ns":v[12],"outer_identity":v[13:15],"inner_identity":v[15:17],
      "session":v[17],"image_sha":v[18].hex(),"manifest_sha":v[19].hex(),"os_sha":v[20].hex(),
      "pins":pins,"capsule_sha":sha,"authority":"PARSED_ONLY_NOT_PROVEN"}

def decode_image(raw, manifest_sha):
    need(type(raw) is bytes and IMAGE.size<=len(raw)<=16*1048576,"IMAGE_SIZE")
    magic,version,count,total,pin=IMAGE.unpack_from(raw)
    need(magic==b"FRA061I1" and version==1 and 0<count<=8192 and 0<=total<=128*1048576 and
         len(raw)==IMAGE.size+count*MEMBER.size and pin.hex()==manifest_sha,"IMAGE_HEADER")
    def string(b):
        need(b"\0" in b,"IMAGE_STRING");p,tail=b.split(b"\0",1)
        need(not any(tail),"IMAGE_STRING_PADDING")
        return p.decode("ascii","strict")
    rows={};charged=0;prior=""
    for i in range(count):
        kind,mode,size,dev,ino,sha,path,target=MEMBER.unpack_from(raw,IMAGE.size+i*MEMBER.size)
        p,t=string(path),string(target)
        need(p.startswith("/") and (p=="/" or all(re.fullmatch(r"[A-Za-z0-9_.+-]+",x) and x not in (".","..") for x in p[1:].split("/"))) and p>prior,"IMAGE_MEMBERSHIP")
        prior=p
        need(kind in (1,2,3,4) and dev>0 and ino>0 and 0<=size<=128*1048576,"IMAGE_ROW")
        need(kind!=4 or p in ("/proc","/sys","/var/tmp"),"IMAGE_PORTAL")
        need((kind==3 and bool(t)) or (kind!=3 and not t),"IMAGE_ALIAS")
        if kind==1:charged+=size
        rows[p]={"kind":kind,"mode":mode,"bytes":size,"dev":dev,"ino":ino,"sha256":sha.hex(),"target":t}
    need(charged==total and all(p in rows for p in ("/","/usr/bin/python3.14","/usr/lib/python3.14","/etc/ld.so.cache",BRIDGE)),"IMAGE_COMPLETE_RUNTIME")
    for p,row in rows.items():
        if p!="/":need(os.path.dirname(p) in rows,"IMAGE_PARENT")
        if row["kind"]==3:
            target=os.path.normpath(os.path.join(os.path.dirname(p),row["target"]))
            need(target in rows and rows[target]["kind"] in (1,2) and
                 (row["dev"],row["ino"])==(rows[target]["dev"],rows[target]["ino"]),"IMAGE_ALIAS_CLOSURE")
    return rows

def proc(pid):
    with open("/proc/%d/stat"%pid,"rb") as stream:raw=stream.read(4097)
    need(len(raw)<=4096,"PROC_CAP")
    fields=raw.rsplit(b")",1)[1].split()
    return int(fields[1]),int(fields[19])

def packet_decode(raw, cap):
    need(len(raw)==PACKET.size,"REG_PACKET_SIZE")
    p=PACKET.unpack(raw)
    need(p[0]==b"FRA061P1" and p[1]==cap["session"] and p[2]==1 and p[12]==cap["work_ns"],"REG_PACKET_IDENTITY")
    need(1<=p[3]<=14 and 0<=p[5]<512 and 1<=p[4]<2**32,"REG_PACKET_TYPE_SEQUENCE")
    return p

def receive(cap,end_ns):
    sock=socket.socket(fileno=os.dup(123))
    try:
        need(sock.type&0xf==socket.SOCK_SEQPACKET,"REG_SOCKET_TYPE")
        need(sock.getsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED)==1,"REG_PASSCRED")
        sock.setblocking(False)
        while time.monotonic_ns()<end_ns:
            if not select.select([sock],[],[],min(.01,max(0,(end_ns-time.monotonic_ns())/1e9)))[0]:continue
            raw,anc,flags,_=sock.recvmsg(PACKET.size,socket.CMSG_SPACE(12)+socket.CMSG_SPACE(16),socket.MSG_CMSG_CLOEXEC)
            passed=[];creds=[]
            for level,kind,data in anc:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    passed.extend(struct.unpack("="+"i"*(len(data)//4),data))
                elif level==socket.SOL_SOCKET and kind==socket.SCM_CREDENTIALS and len(data)==12:creds.append(struct.unpack("=iii",data))
                else:flags|=socket.MSG_CTRUNC
            for fd in passed:os.close(fd)
            need(not flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC) and not passed and len(creds)==1 and
                 creds[0]==(os.getppid(),0,0),"REG_NATIVE_CREDENTIALS")
            return packet_decode(raw,cap)
        raise Refused("REG_DEADLINE")
    finally:sock.close()

def command(cap,kind,ack):
    if NATIVE is not None:
        need((kind,ack) in ((12,13),(9,10)),"REG_PUBLIC_COMMAND")
        result=NATIVE.fr_public_command(kind)
        need(result==0,"REG_PUBLIC_COMMAND:%d"%result)
        return
    raise Refused("REG_ACTUAL_NATIVE_SESSION_REQUIRED")

def startup(expected_sha):
    global ADMISSION
    cap=decode_capsule(held(100,expected_sha,CAP.size),expected_sha)
    need(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and dict(os.environ)==ENV,"A061_START_ENV")
    need(os.getuid()==cap["uid"] and os.getgid()==cap["gid"] and os.getgroups()==[],"A061_START_CREDENTIALS")
    with open("/proc/self/status",encoding="ascii") as stream:status=stream.read(65537)
    need(len(status)<=65536 and all(re.search(r"^"+k+r":\s+0000000000000000$",status,re.M) for k in
          ("CapInh","CapPrm","CapEff","CapBnd","CapAmb")) and re.search(r"^NoNewPrivs:\s+1$",status,re.M),"A061_PREINTERPRETER_CAPABILITIES")
    for role,fd in zip(SOURCE_ROLES,SOURCE_FDS):held(fd,cap["pins"][role],16*1048576 if role=="native" else 1048576)
    image=decode_image(held(111,cap["image_sha"],16*1048576),cap["manifest_sha"])
    expected_limits={resource.RLIMIT_AS:48*1048576,resource.RLIMIT_NOFILE:512,resource.RLIMIT_FSIZE:2147483648,resource.RLIMIT_CORE:0}
    for kind,value in expected_limits.items():need(resource.getrlimit(kind)==(value,value),"A061_PREINTERPRETER_LIMITS")
    cpu=180 if cap["mode_id"] in (1,4) else 1200
    need(resource.getrlimit(resource.RLIMIT_CPU)==(cpu,cpu),"A061_PREINTERPRETER_CPU")
    need(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=48*1048576 and
         resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024<=268435456,"A061_RAW_HISTORICAL_RSS_CAP")
    cap["image"]=image
    validate_runtime_input(cap);check_loaded_runtime(cap)
    library=native_bridge(cap);start=NativePacket()
    result=library.fr_session_start(123,expected_sha.encode("ascii"),ctypes.byref(start))
    need(result==0,"REG_NATIVE_SESSION_START:%d"%result)
    p=packet_decode(bytes(start),cap)
    parent,birth=proc(os.getpid());outer_parent,outer_birth=proc(parent)
    need(p[3]==1 and p[6]==os.getpid() and p[7]==parent and p[10]==birth and p[11]==outer_birth and
         p[8]==p[9]==0,"A061_NATIVE_CLONE_START")
    cap.update(image=image,authority="AUTHENTIC_NATIVE_START_AND_HELD_ROOT_INPUTS_VALIDATED",native_start_packet=list(p[2:]))
    ADMISSION=cap;check_loaded_runtime(cap)
    return cap

def validate_runtime_input(cap):
    """Exact held117 -> image111 correspondence in the same protected view.
    Does not recapture host bytes or select a pin from the data being checked.
    Original logical interpreter/stdlib/soname/cache/loader grammar is retained.
    """
    raw=held(117,cap["manifest_sha"],1048576)
    def unique(items):
        result={}
        for key,value in items:
            need(key not in result,"RUNTIME_INPUT_DUPLICATE")
            result[key]=value
        return result
    doc=json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _ : (_ for _ in ()).throw(Refused("RUNTIME_INPUT_NONFINITE")))
    need(type(doc) is dict and set(doc)=={"schema","runtime","os_files","aliases"} and
         doc["schema"]=="friday.a061.runtime-image-input.v1","RUNTIME_INPUT_SCHEMA")
    runtime=doc["runtime"]
    need(type(runtime) is dict and set(runtime)=={"schema","interpreter","stdlib","files","sonames","loader_alias"} and
         runtime["schema"]=="friday.browser3.trusted-runtime.v1" and
         runtime["interpreter"]=="/usr/bin/python3.14" and runtime["stdlib"]=="/usr/lib/python3.14" and
         type(runtime["files"]) is dict and type(doc["os_files"]) is dict,
         "RUNTIME_INPUT_LOGICAL_GRAMMAR")
    files=dict(runtime["files"])
    need(not set(files)&set(doc["os_files"]),"RUNTIME_INPUT_ROLE_COLLISION")
    files.update(doc["os_files"])
    imagefiles={p:r for p,r in cap["image"].items() if r["kind"]==1}
    need(set(files)==set(imagefiles) and BRIDGE in files and
         {"/etc/nsswitch.conf","/etc/resolv.conf","/etc/hosts"}<=set(doc["os_files"])<=
         {"/etc/nsswitch.conf","/etc/resolv.conf","/etc/hosts","/etc/gai.conf","/etc/services","/etc/protocols"},
         "RUNTIME_INPUT_COMPLETE_MEMBERSHIP")
    for p,row in files.items():
        need(type(row) is dict and set(row)=={"bytes","sha256"} and type(row["bytes"]) is int and
             row["bytes"]==imagefiles[p]["bytes"] and row["sha256"]==imagefiles[p]["sha256"],
             "RUNTIME_SOURCE_TO_IMAGE_BYTES")
    names=runtime["sonames"];loader=runtime["loader_alias"]
    need(type(names) is dict and all(type(k) is str and re.fullmatch(r"[A-Za-z0-9_.+-]+",k) and
         type(v) is str and v in runtime["files"] for k,v in names.items()) and
         type(loader) is dict and set(loader)=={"path","target"} and loader["target"] in runtime["files"] and
         type(doc["aliases"]) is dict and set(doc["aliases"])=={loader["path"]},"RUNTIME_SOURCE_TO_IMAGE_ROLES")
    expected_aliases={"/lib":"usr/lib","/lib64":"usr/lib64",**doc["aliases"]}
    for p,target in doc["aliases"].items():
        for prefix,replacement in (("/lib","usr/lib"),("/lib64","usr/lib64")):
            if p.startswith(prefix+"/"):expected_aliases["/"+replacement+p[len(prefix):]]=target
    need({p:r["target"] for p,r in cap["image"].items() if r["kind"]==3}==expected_aliases,
         "RUNTIME_SOURCE_TO_IMAGE_ALIAS_MEMBERSHIP")
    OBSERVATIONS.append({"stage":"held117_to_same_image111_exact_bytes_roles","manifest_sha256":cap["manifest_sha"],
                         "image_sha256":cap["image_sha"],"files":len(files),"authority":"EXTERNAL_CAPSULE_NOT_MINTED"})

def check_loaded_runtime(cap):
    expected={"/usr/lib/python314.zip","/usr/lib/python3.14","/usr/lib/python3.14/lib-dynload"}
    need(set(sys.path)<=expected and "/usr/lib/python3.14" in sys.path and not os.path.lexists("/usr/lib/python314.zip"),"RUNTIME_IMPORT_VIEW")
    rows=cap["image"];known={(os.major(r["dev"]),os.minor(r["dev"]),r["ino"]) for r in rows.values() if r["kind"]==1}
    with open("/proc/self/maps","rt",encoding="ascii") as stream:text=stream.read(1048577)
    need(len(text)<=1048576,"RUNTIME_MAPS_CAP")
    for line in text.splitlines():
        parts=line.split(maxsplit=5);need(len(parts)>=5,"RUNTIME_MAPS_FORMAT")
        perms,device,inode=parts[1],parts[3],int(parts[4]);name=parts[5] if len(parts)>5 else ""
        if inode:
            major,minor=(int(v,16) for v in device.split(":"))
            need((major,minor,inode) in known,"RUNTIME_MAPPING_MEMBERSHIP")
        elif "x" in perms:need(name in ("[vdso]","[vsyscall]"),"RUNTIME_UNKNOWN_EXEC_MAPPING")
    OBSERVATIONS.append({"stage":"same_protected_runtime_mappings","maps_sha256":hashlib.sha256(text.encode()).hexdigest(),"ordinary_host_environment_is_runtime_authority":False})

class Child(ctypes.Structure):
    _fields_=[("pid",ctypes.c_int32),("pidfd",ctypes.c_int32),("role",ctypes.c_int32),("state",ctypes.c_int32),
              ("birth",ctypes.c_uint64),("status",ctypes.c_int32),("creation_errno",ctypes.c_int32)]

class NativePacket(ctypes.Structure):
    _pack_=1
    _fields_=[("magic",ctypes.c_uint8*8),("session",ctypes.c_uint8*32),
        ("version",ctypes.c_uint32),("type",ctypes.c_uint32),("sequence",ctypes.c_uint32),("role",ctypes.c_uint32),
        ("pid",ctypes.c_int32),("owner",ctypes.c_int32),("status",ctypes.c_int32),("detail",ctypes.c_int32),
        ("birth",ctypes.c_uint64),("owner_birth",ctypes.c_uint64),("deadline_ns",ctypes.c_uint64)]

class NativeObservation(ctypes.Structure):
    _fields_=[(name,ctypes.c_int32) for name in ("owner","origin","uid","gid","pid","pidfd","role","state")]+[
        (name,ctypes.c_uint64) for name in ("owner_birth","origin_birth","birth")]+[
        (name,ctypes.c_uint32) for name in ("next_sequence","session_ready","creation_poisoned","wait_observed",
            "status_known","cleanup_reaped","handle_closed","stop_attempted")]+[
        ("status",ctypes.c_int32),("reserved",ctypes.c_int32)]

def native_bridge(cap):
    global NATIVE
    if NATIVE is not None:
        check_loaded_runtime(cap);return NATIVE
    row=cap["image"].get(BRIDGE)
    need(row is not None and row["kind"]==1 and row["sha256"]==cap["pins"]["bridge"],"BRIDGE_IMAGE_SOURCE_IDENTITY")
    fd=os.open(BRIDGE,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd);need((st.st_dev,st.st_ino)==(row["dev"],row["ino"]),"BRIDGE_INODE")
        held(fd,cap["pins"]["bridge"],1048576)
    finally:os.close(fd)
    # Named dlopen resolves inside the kernel-protected, fully enumerated chroot;
    # it opens the SAME bind-mounted sealed inode, not the mutable host path.
    lib=ctypes.PyDLL(BRIDGE,use_errno=True)
    lib.fr_own_spawn.argtypes=[ctypes.c_int,ctypes.c_uint,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.POINTER(Child)]
    lib.fr_own_spawn.restype=ctypes.c_int
    lib.fr_own_wait.argtypes=[ctypes.c_int,ctypes.POINTER(Child),ctypes.c_int];lib.fr_own_wait.restype=ctypes.c_int
    lib.fr_own_stop.argtypes=[ctypes.c_int,ctypes.POINTER(Child),ctypes.c_uint64];lib.fr_own_stop.restype=ctypes.c_int
    lib.fr_fixture_fork.argtypes=[ctypes.c_int];lib.fr_fixture_fork.restype=ctypes.c_int
    lib.fr_fixture_reap.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_int];lib.fr_fixture_reap.restype=ctypes.c_int
    lib.fr_fixture_wait.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
    lib.fr_fixture_wait.restype=ctypes.c_int
    lib.fr_public_command.argtypes=[ctypes.c_uint];lib.fr_public_command.restype=ctypes.c_int
    lib.fr_session_start.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.POINTER(NativePacket)];lib.fr_session_start.restype=ctypes.c_int
    lib.fr_session_observe.argtypes=[ctypes.POINTER(NativeObservation)];lib.fr_session_observe.restype=ctypes.c_int
    lib.fr_own_release.argtypes=[ctypes.POINTER(Child)];lib.fr_own_release.restype=ctypes.c_int
    lib.fr_fixture_observe.argtypes=[ctypes.c_int,ctypes.POINTER(NativeObservation)];lib.fr_fixture_observe.restype=ctypes.c_int
    lib.fr_fixture_stop.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64];lib.fr_fixture_stop.restype=ctypes.c_int
    check_loaded_runtime(cap);NATIVE=lib;return lib

def enable_fixture_forks(cap,lib):
    need(cap["mode_id"]==1,"FIXTURE_FORK_ROLE")
    original_fork,original_wait=os.fork,os.waitpid
    def fork():
        pid=lib.fr_fixture_fork(123)
        need(pid>=0,"FIXTURE_NATIVE_FORK:%d"%pid)
        return pid
    def waitpid(pid,flags):
        status=ctypes.c_int()
        done=lib.fr_fixture_wait(123,pid,flags,ctypes.byref(status))
        need(done>=0,"FIXTURE_NATIVE_WAIT_ACK:%d"%done)
        return done,status.value
    os.fork,os.waitpid=fork,waitpid
    return lambda:(setattr(os,"fork",original_fork),setattr(os,"waitpid",original_wait))

def normalized(cap,executor):
    result=dict(cap)
    result["sources"]={role:{"fd":fd,"path":path,"sha256":cap["pins"][role]}
                       for role,(fd,path) in executor.HELD_ROLES.items()}
    return result

def production_resources(cap):
    def read(path,capbytes=65536):
        with open(path,encoding="ascii") as stream:text=stream.read(capbytes+1)
        need(len(text)<=capbytes,"RESOURCE_READ_CAP");return text.strip()
    need(read("/proc/self/cgroup")=="0::/friday-browser3-a061-g1/inner","RESOURCE_CGROUP_MEMBERSHIP")
    root="/sys/fs/cgroup/friday-browser3-a061-g1"
    out={"aggregate_envelope":268435456,"outer_budget":67108864,"inner_budget":201326592,"coordinator_as":50331648,"worker_as_each":50331648}
    for leaf,limit,pids in (("outer",67108864,1),("inner",201326592,4)):
        expected={"memory.max":str(limit),"memory.swap.max":"0","memory.oom.group":"1","pids.max":str(pids),"cpu.max":"max 100000"}
        values={k:read(root+"/"+leaf+"/"+k) for k in expected}
        need(values==expected,"RESOURCE_KERNEL_ENVELOPE")
        current=read(root+"/"+leaf+"/memory.current")
        need(current.isdigit() and int(current)<limit,"RESOURCE_MEMORY_CURRENT")
        values["memory.current"]=int(current)
        for key in ("cpu.pressure","io.pressure","cpuset.cpus.effective"):
            try:values[key]=read(root+"/"+leaf+"/"+key)
            except FileNotFoundError:values[key]=None
        out[leaf]=values
    need(sum(out[k]["memory.current"] for k in ("outer","inner"))<268435456,"RESOURCE_AGGREGATE_CURRENT")
    out["cpu_affinity"]=sorted(os.sched_getaffinity(0));need(bool(out["cpu_affinity"]),"RESOURCE_CPU_AFFINITY")
    mem=read("/proc/meminfo");m=re.search(r"^MemAvailable:\s+(\d+) kB$",mem,re.M)
    need(m is not None and int(m[1])*1024>=268435456,"MEMORY_SHORTFALL")
    disk=os.statvfs("/var/tmp");need(disk.f_bavail*disk.f_frsize>=2147483648+67108864,"DISK_SHORTFALL")
    out.update(memory_available_bytes=int(m[1])*1024,var_tmp_available_bytes=disk.f_bavail*disk.f_frsize,
       raw_self_peak_KiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
       raw_children_peak_KiB=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    # Every ancestor, including the 256MiB run parent, is measured independently.
    ancestor=root;out["ancestors"]=[]
    while True:
        maximum,current=read(ancestor+"/memory.max"),read(ancestor+"/memory.current")
        need(current.isdigit() and (maximum=="max" or maximum.isdigit()),"RESOURCE_ANCESTOR_VALUES")
        required=201326592-out["inner"]["memory.current"] if ancestor==root else 268435456
        need(maximum=="max" or int(maximum)-int(current)>=required,"CGROUP_ANCESTOR_MEMORY_SHORTFALL")
        out["ancestors"].append({"path":ancestor,"memory.max":maximum,"memory.current":int(current),"required_headroom":required})
        if ancestor=="/sys/fs/cgroup":break
        ancestor=os.path.dirname(ancestor)
    return out
