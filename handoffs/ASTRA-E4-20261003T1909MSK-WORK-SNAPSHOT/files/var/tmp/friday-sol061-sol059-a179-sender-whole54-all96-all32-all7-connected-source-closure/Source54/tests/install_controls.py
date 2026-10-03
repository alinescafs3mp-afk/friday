"""Pure-data install control applicability, derived before execution."""
import hashlib
import builtins
import errno
import io
import os
import stat
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import AbstractContextManager
from install import (journal_plan_from_grant, INSTALL_PHASES, REMOVE_PHASES,
    install_fault_points, removal_fault_points, install_effect_ids, removal_effect_ids,
    effect_phase_map, effect_object_contract, bootstrap_effects, ZERO)
from install_bootstrap import NAMESPACE
from pinned_fs import NativeBackend


def graph_plan():
    package, manifest = "1"*64, "2"*64
    snapshot="/usr/libexec/friday/quality-gate-toolchain-v1/"+manifest
    broker="/usr/libexec/friday/quality-gate-broker-v1/"+package
    names=("canonical","pinned_fs","provenance","archive","manifest","assemble","install_bootstrap","install","ledger","custody_linux","broker_runtime","broker_bootstrap")
    paths=["broker/"+name+".py" for name in names]+["broker/package-index.v1.json","snapshot/candidate/tools/quality_gate.py","snapshot/golden/input","snapshot/rootfs/data"]
    members=[{"source":path,"destination":snapshot+path[len("snapshot"):] if path.startswith("snapshot/") else broker+path[len("broker"):],"type":"file"} for path in sorted(paths)]
    policy=("#1000 ALL=(root) NOPASSWD:NOSETENV: /usr/bin/python3.14 -I -B -S "+broker+"/broker_bootstrap.py run-v1\n").encode("ascii")
    grant={"transaction_id":"transaction-one","package_index_sha256":package,"snapshot_manifest_sha256":manifest,
        "retained_directories":[],"previous_removed_journal_sha256":ZERO,
        "authority_id":"synthetic-install","caller_uid":1000,"policy_sha256":hashlib.sha256(policy).hexdigest(),
        "members":members,"authority":{"ledger_directory":"/"+NAMESPACE+"/attempts/"+"a"*40+"/1","scratch_parent":"/"+NAMESPACE+"/scratch"},"bootstrap_trust":{},
        "snapshot_directories":[{"path":p,"uid":0,"gid":0,"mode":0o555} for p in ("candidate","candidate/tools","golden","rootfs")],
        "snapshot_envelope":[{"name":name,"size":1,"sha256":ZERO,"uid":0,"gid":0,"mode":0o444} for name in ("manifest.v1.json","provenance.v1.json")]}
    identity={"grant":grant,"package_index_sha256":package,"install_grant_sha256":ZERO}
    return journal_plan_from_grant(grant,identity,ZERO)


def signature(point,plan):
    return point.replace(plan.expected_package_sha256,"<package>").replace(plan.grant["snapshot_manifest_sha256"],"<manifest>")


def declared_faults(operation):
    plan=graph_plan()
    points=install_fault_points(plan) if operation=="install" else removal_fault_points(plan)
    return sorted(signature(p,plan) for p in points)


ARTIFACT_DIMENSIONS=("inode","type","mount","digest")
BATCH_SIZE=32
ROLLOVER_REFUSALS = ("missing-list", "foreign-path", "changed-grant-identity", "wrong-predecessor-pin", "missing-predecessor")
RETAINED_IDENTITY_AXES = ("dev", "ino", "mount", "uid", "gid", "mode", "nlink", "type")
POLICY_SUBSTITUTIONS = ("uid", "gid", "mode", "inode", "mount", "nlink", "symlink", "fifo", "socket", "device", "digest")
PENDING_COPY_REMOVE_STATES = ("empty-stage", "empty-published", "full-applied", "empty-granted",
    "partial", "wrong-digest", "changed-inode", "changed-metadata", "identity-drift-on-read",
    "pre-intent-foreign", "partial-then-granted", "wrong-then-granted")
PENDING_COPY_REMOVE_POSITIVES = ("empty-stage", "empty-published", "full-applied", "empty-granted")


class InertSourceSubstitution(NativeBackend):
    """Changes one actual held source artifact observation, never request hashes."""
    def __init__(self, wrapped, path, dimension):
        super().__init__(); self.wrapped,self.path,self.dimension=wrapped,path,dimension
        self.seen=[]
    def matched(self,fd,name=None):
        path=os.readlink("/proc/self/fd/"+str(fd))
        if name is not None: path=path+"/"+name
        return path==self.path
    def rewrite(self,value):
        self.seen.append(self.dimension)
        fields={name:getattr(value,name) for name in ("st_dev","st_ino","st_mode","st_uid","st_gid","st_nlink","st_size","st_mtime_ns","st_ctime_ns")}
        if self.dimension=="inode": fields["st_ino"]+=1
        elif self.dimension=="type": fields["st_mode"]=stat.S_IFDIR|stat.S_IMODE(value.st_mode)
        return SimpleNamespace(**fields)
    def stat(self,name,fd):
        value=self.wrapped.stat(name,fd)
        return self.rewrite(value) if self.matched(fd,name) and self.dimension in ("inode","type") else value
    def fstat(self,fd):
        value=self.wrapped.fstat(fd)
        return self.rewrite(value) if self.matched(fd) and self.dimension in ("inode","type") else value
    def mount(self,fd):
        value=self.wrapped.mount(fd)
        if self.matched(fd) and self.dimension=="mount": self.seen.append("mount"); return value+1
        return value
    def read(self,fd,length):
        raw=self.wrapped.read(fd,length)
        if raw and self.matched(fd) and self.dimension=="digest":
            self.seen.append("digest"); return bytes([raw[0]^1])+raw[1:]
        return raw


def batches(name,rows):
    return {name+":batch%03d"%(offset//BATCH_SIZE):rows[offset:offset+BATCH_SIZE]
        for offset in range(0,len(rows),BATCH_SIZE)}


def retain_prefixes(root, selected, snapshots, trace):
    """Exact selected prefix bytes and full modeled identity graph, not hashes alone."""
    directory=Path(root).parent/"prefix-evidence"; directory.mkdir(mode=0o700)
    blobs=directory/"bytes"; blobs.mkdir(mode=0o700)
    records=[]; written=set()
    for point in selected:
        snapshot=snapshots[point]; graph={}
        for path,node in sorted(snapshot["nodes"].items()):
            raw=node["raw"]; pin=hashlib.sha256(raw).hexdigest()
            if pin not in written:
                with (blobs/pin).open("xb") as stream: stream.write(raw)
                os.chmod(blobs/pin,0o600); written.add(pin)
            graph[path]={key:value for key,value in node.items() if key!="raw"}
            graph[path]["raw_sha256"]=pin
        records.append({"point":point,"hook_index":snapshot["hook_index"],"identity_graph":graph,
            "modeled_metadata":snapshot["metadata"],"crash_model":"first-occurrence prefix; old descriptors/leases discarded"})
    with (directory/"catalogue.json").open("x",encoding="ascii") as stream:
        json.dump({"ordered_source_hooks":trace,"selected_prefixes":records},stream,sort_keys=True,separators=(",",":"))
    os.chmod(directory/"catalogue.json",0o600)
    return str(directory/"catalogue.json")


class MemorySyscallReplay(AbstractContextManager):
    """Explicit descriptor/name/stat/mount model for the actual source wrappers.

    Only the one exact fake-root subtree and synthetic fd numbers are redirected.
    All other calls retain the fenced ordinary-file host capability. No native,
    privileged, network, process or special-object capability is re-enabled.
    """
    def __init__(self, root):
        self.root = os.fspath(root)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.nodes={self.root:self.node("directory",0o700)}
        self.fds={}; self.nextfd=100000; self.nextino=100
        self.saved={}; self.clock=10
    def node(self, kind, mode, raw=b""):
        return {"kind":kind,"mode":mode,"raw":raw,"ino":len(getattr(self,"nodes",{}))+100,
            "uid":getattr(self,"uid",1000),"gid":getattr(self,"gid",1000),"nlink":1,"time":getattr(self,"clock",10)}
    def path(self, value, fd=None):
        if isinstance(value, bytes): value=os.fsdecode(value)
        if not isinstance(value,str): return None
        if not value.startswith("/"):
            if fd in self.fds: value=self.fds[fd]["path"]+"/"+value
            else: return None
        value=os.path.normpath(value)
        return value if value==self.root or value.startswith(self.root+"/") else None
    def info(self,node,path):
        kind=stat.S_IFDIR if node["kind"]=="directory" else stat.S_IFREG
        nlink=(2+sum(n["kind"]=="directory" and p.rpartition("/")[0]==path for p,n in self.nodes.items())) if node["kind"]=="directory" else node["nlink"]
        return SimpleNamespace(st_dev=42,st_ino=node["ino"],st_mode=kind|node["mode"],st_uid=node["uid"],st_gid=node["gid"],
            st_nlink=nlink,st_size=len(node["raw"]),st_mtime_ns=node["time"],st_ctime_ns=node["time"])
    def get(self,path):
        if path not in self.nodes: raise FileNotFoundError(errno.ENOENT,"absent",path)
        return self.nodes[path]
    def touch(self,path):
        self.clock+=1
        if path in self.nodes: self.nodes[path]["time"]=self.clock
    def open(self,value,flags,mode=0o777,*,dir_fd=None):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.open"](value,flags,mode,dir_fd=dir_fd)
        if flags&os.O_CREAT:
            if path in self.nodes and flags&os.O_EXCL: raise FileExistsError(path)
            if path not in self.nodes:
                parent=self.get(path.rpartition("/")[0])
                if parent["kind"]!="directory": raise NotADirectoryError(path)
                self.nextino+=1; self.nodes[path]=self.node("file",mode)
                self.nodes[path]["ino"]=self.nextino; self.touch(path.rpartition("/")[0])
        node=self.get(path)
        if flags&os.O_DIRECTORY and node["kind"]!="directory": raise NotADirectoryError(path)
        if flags&os.O_TRUNC: node["raw"]=b""; self.touch(path)
        fd=self.nextfd; self.nextfd+=1; self.fds[fd]={"node":node,"path":path,"offset":0}
        return fd
    def close(self,fd):
        if fd in self.fds: self.fds.pop(fd); return
        return self.saved["os.close"](fd)
    def dup(self,fd):
        if fd not in self.fds: return self.saved["os.dup"](fd)
        new=self.nextfd; self.nextfd+=1; self.fds[new]=dict(self.fds[fd]); return new
    def fstat(self,fd):
        if fd not in self.fds: return self.saved["os.fstat"](fd)
        item=self.fds[fd]; return self.info(item["node"],item["path"])
    def stat(self,value,*,dir_fd=None,follow_symlinks=True):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.stat"](value,dir_fd=dir_fd,follow_symlinks=follow_symlinks)
        return self.info(self.get(path),path)
    def read(self,fd,length):
        if fd not in self.fds: return self.saved["os.read"](fd,length)
        item=self.fds[fd]; offset=item["offset"]; raw=item["node"]["raw"][offset:offset+length]
        item["offset"]+=len(raw); return raw
    def pread(self,fd,length,offset):
        if fd not in self.fds: return self.saved["os.pread"](fd,length,offset)
        return self.fds[fd]["node"]["raw"][offset:offset+length]
    def write(self,fd,value):
        if fd not in self.fds: return self.saved["os.write"](fd,value)
        item=self.fds[fd]; raw=bytes(value); offset=item["offset"]; old=item["node"]["raw"]
        item["node"]["raw"]=old[:offset]+raw+old[offset+len(raw):]; item["offset"]+=len(raw); self.touch(item["path"])
        return len(raw)
    def fsync(self,fd):
        if fd not in self.fds: return self.saved["os.fsync"](fd)
    def listxattr(self,value,*args,**kwargs):
        if value in self.fds if isinstance(value,int) else self.path(value) is not None: return []
        return self.saved["os.listxattr"](value,*args,**kwargs)
    def readlink(self,value,*,dir_fd=None):
        prefix="/proc/self/fd/"
        if isinstance(value,str) and value.startswith(prefix) and value[len(prefix):].isdigit() and int(value[len(prefix):]) in self.fds:
            return self.fds[int(value[len(prefix):])]["path"]
        return self.saved["os.readlink"](value,dir_fd=dir_fd)
    def mkdir(self,value,mode=0o777,*,dir_fd=None):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.mkdir"](value,mode,dir_fd=dir_fd)
        if path in self.nodes: raise FileExistsError(path)
        self.get(path.rpartition("/")[0]); self.nextino+=1; self.nodes[path]=self.node("directory",mode)
        self.nodes[path]["ino"]=self.nextino; self.touch(path.rpartition("/")[0])
    def listdir(self,value="."):
        path=self.fds[value]["path"] if isinstance(value,int) and value in self.fds else self.path(value)
        if path is None: return self.saved["os.listdir"](value)
        self.get(path)
        return sorted(p.rsplit("/",1)[-1] for p in self.nodes if p.rpartition("/")[0]==path)
    def rename(self,left,right,*,src_dir_fd=None,dst_dir_fd=None):
        source,target=self.path(left,src_dir_fd),self.path(right,dst_dir_fd)
        if source is None or target is None: return self.saved["os.rename"](left,right,src_dir_fd=src_dir_fd,dst_dir_fd=dst_dir_fd)
        self.get(source); self.get(target.rpartition("/")[0])
        if target in self.nodes: self.nodes[target]["nlink"]=0
        for path in sorted(tuple(self.nodes),key=len):
            if path==source or path.startswith(source+"/"): self.nodes[target+path[len(source):]]=self.nodes.pop(path)
        for item in self.fds.values():
            if item["path"]==source or item["path"].startswith(source+"/"): item["path"]=target+item["path"][len(source):]
        self.touch(source.rpartition("/")[0]); self.touch(target.rpartition("/")[0])
    def unlink(self,value,*,dir_fd=None):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.unlink"](value,dir_fd=dir_fd)
        node=self.get(path)
        if node["kind"]=="directory": raise IsADirectoryError(path)
        node["nlink"]=0; self.nodes.pop(path); self.touch(path.rpartition("/")[0])
    def rmdir(self,value,*,dir_fd=None):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.rmdir"](value,dir_fd=dir_fd)
        if self.listdir(path): raise OSError(errno.ENOTEMPTY,"nonempty",path)
        self.get(path)["nlink"]=0; self.nodes.pop(path); self.touch(path.rpartition("/")[0])
    def fileopen(self,value,mode="r",*args,**kwargs):
        prefix="/proc/self/fdinfo/"
        if isinstance(value,str) and value.startswith(prefix) and value[len(prefix):].isdigit() and int(value[len(prefix):]) in self.fds:
            return io.BytesIO(b"mnt_id:\t77\n") if "b" in mode else io.StringIO("mnt_id:\t77\n")
        path=self.path(os.fspath(value)) if not isinstance(value,int) else None
        if path is None: return self.saved["builtins.open"](value,mode,*args,**kwargs)
        writing=any(c in mode for c in "wax+")
        if "x" in mode and path in self.nodes: raise FileExistsError(path)
        if path not in self.nodes and writing:
            self.get(path.rpartition("/")[0]); self.nextino+=1
            self.nodes[path]=self.node("file",0o600); self.nodes[path]["ino"]=self.nextino
        node=self.get(path)
        if not writing: return io.BytesIO(node["raw"]) if "b" in mode else io.StringIO(node["raw"].decode("ascii"))
        replay=self
        base=io.BytesIO if "b" in mode else io.StringIO
        class Buffer(base):
            def close(self):
                if not self.closed:
                    value=self.getvalue(); node["raw"]=value if isinstance(value,bytes) else value.encode("ascii")
                    replay.touch(path)
                super().close()
        return Buffer(b"" if "b" in mode else "") if "w" in mode or "x" in mode else Buffer(node["raw"] if "b" in mode else node["raw"].decode("ascii"))
    def chmod(self,value,mode,*,dir_fd=None,follow_symlinks=True):
        path=self.path(value,dir_fd)
        if path is None: return self.saved["os.chmod"](value,mode,dir_fd=dir_fd,follow_symlinks=follow_symlinks)
        self.get(path)["mode"]=mode; self.touch(path)
    def __enter__(self):
        for name in ("open","close","dup","fstat","stat","read","pread","write","fsync","listxattr","readlink","mkdir","listdir","rename","unlink","rmdir","chmod"):
            self.saved["os."+name]=getattr(os,name); setattr(os,name,getattr(self,name))
        self.saved["builtins.open"]=builtins.open; builtins.open=self.fileopen
        self.saved["io.open"]=io.open; io.open=self.fileopen
        return self
    def __exit__(self,*unused):
        for key,value in self.saved.items():
            module,name=key.split(".",1); setattr({"os":os,"builtins":builtins,"io":io}[module],name,value)
        if self.fds: raise AssertionError("unclosed memory descriptors: "+str(sorted(self.fds)))
    def snapshot(self,backend):
        # Bytes are immutable and shared; only small node/metadata mappings copy.
        return {"nodes":{path:dict(node) for path,node in self.nodes.items()},
            "metadata":backend.export_state(),"clock":self.clock,"nextino":self.nextino}
    def restore(self,snapshot):
        if self.fds: raise AssertionError("restore requires all prior descriptors closed")
        self.nodes={path:dict(node) for path,node in snapshot["nodes"].items()}
        self.clock=snapshot["clock"]; self.nextino=snapshot["nextino"]


def install_prefixes(plan, backend, replay):
    from install import Installer
    expected=set(install_fault_points(plan)); snapshots={}; trace=[]
    def record(point):
        trace.append(point)
        if point in expected:
            if point in snapshots: raise AssertionError("duplicate actual install boundary: "+point)
            snapshots[point]=replay.snapshot(backend)
            snapshots[point]["hook_index"]=len(trace)-1
    Installer(plan,backend,record).install()
    if set(snapshots)!=expected: raise AssertionError("missing actual install boundaries: "+str(sorted(expected-set(snapshots))))
    return snapshots,trace


def remove_prefixes(plan, backend, replay):
    from uninstall import Remover
    expected=set(removal_fault_points(plan)); snapshots={}; trace=[]
    def record(point):
        trace.append(point)
        if point in expected:
            if point in snapshots: raise AssertionError("duplicate actual remove boundary: "+point)
            snapshots[point]=replay.snapshot(backend)
            snapshots[point]["hook_index"]=len(trace)-1
    Remover(plan,backend,record).remove()
    if set(snapshots)!=expected: raise AssertionError("missing actual remove boundaries: "+str(sorted(expected-set(snapshots))))
    return snapshots,trace


def applicability():
    """Complete boundary universe; no request-hash substitutions count as artifacts.

    A boundary before the first journal remains applicable to removal continuation;
    artifact attacks are N/A only where no transaction-owned artifact can exist.
    Every other row remains mandatory even when the diagnostic run did not reach it.
    """
    plan=graph_plan()
    faults=declared_faults("install")
    # The externally pinned held source artifact exists at every boundary,
    # including pre-journal preparation. No boundary is blanket N/A.
    artifacts=[key+":"+dimension for key in faults for dimension in ARTIFACT_DIMENSIONS]
    return {"install_faults":faults,"remove_faults":declared_faults("remove"),
        "artifact_substitution":sorted(artifacts),"artifact_na":[],
        "remove_after_install_crash":faults}


def install_contract():
    sets=applicability()
    matrices={"install-phase-identity-remove":sorted(p+":"+a for p in INSTALL_PHASES for a in ("identity","remove")),
        "install-rollover-retained-authority": sorted(list(ROLLOVER_REFUSALS) + ["retained-" + key for key in RETAINED_IDENTITY_AXES])}
    for name,rows in (("install-before-after-phase-effect",sets["install_faults"]),("install-artifact-substitution",sets["artifact_substitution"]),("remove-after-every-install-crash",sets["remove_after_install_crash"])):
        matrices.update(batches(name,rows))
    return {"matrices":matrices,
        "required_observations":sorted(["install:complete-policy-last","install:external-pins-mode",
            "install-negative:actual-filesystem-methods","install-negative:coherent-journal-graph",
            "install-negative:pre-policy-stage-residue","install-negative:pending-create-preserved",
            "install-negative:created-directory-observation",
            "install-negative:three-transaction-rollover-old-refusal"] +
            ["install-rollover-negative:" + key for key in matrices["install-rollover-retained-authority"]] +
            ["install-pending-copy:" + key for key in ("empty-stage", "empty-published", "full-applied", "partial",
                "wrong-digest", "changed-inode", "changed-metadata", "identity-drift-on-read", "pre-intent-foreign", "empty-granted", "partial-then-granted", "wrong-then-granted")] +
            ["install-pending-copy-lineage:" + key for key in ("missing", "null", "foreign", "nonabsent-create")] +
            ["install-pending-copy-semantic:" + key for key in ("unchanged-positive", "absent-owned-object", "member-create-completion-missing")])}


def remove_contract():
    matrices=batches("remove-before-after-phase-effect",declared_faults("remove"))
    matrices["remove-phase-identity-no-republish"]=sorted(p+":"+a for p in REMOVE_PHASES for a in ("foreign","resume"))
    matrices["remove-policy-substitution"] = sorted(POLICY_SUBSTITUTIONS)
    matrices["remove-direct-pending-copy"] = sorted(PENDING_COPY_REMOVE_STATES)
    return {"matrices":matrices,
        "required_observations":sorted(["remove:revoke-first-zero-residue-audit","remove:exact-live-three-documents",
            "remove-negative:fresh-private-suffix-fence","remove-negative:locked-journal-promotion"] +
            ["remove-negative:policy-substitution-" + key for key in POLICY_SUBSTITUTIONS] +
            ["remove-direct-pending-copy:" + key for key in PENDING_COPY_REMOVE_STATES])}
