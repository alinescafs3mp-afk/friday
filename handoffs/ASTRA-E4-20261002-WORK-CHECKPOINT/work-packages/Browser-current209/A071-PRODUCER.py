"""Future separately admitted root/runtime producer. SOURCE ONLY; NEVER RUN NOW.
Creates a complete readonly runtime view from the actual approved held bytes.
No root capsule is minted or approved here. finish() needs an independent exact
root-sealed admission capsule after the immutable view/index have been reviewed.
"""
import ctypes
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import types

MS_RDONLY=1;MS_NOSUID=2;MS_NODEV=4;MS_NOEXEC=8;MS_REMOUNT=32;MS_BIND=4096;MS_REC=16384;MS_PRIVATE=1<<18
AT_EMPTY_PATH=4096;MOUNT_ATTR_RDONLY=1;MOUNT_ATTR_NOSUID=2;MOUNT_ATTR_NODEV=4
SEALS=fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
ALLOWED_OS={"/etc/nsswitch.conf","/etc/resolv.conf","/etc/hosts","/etc/gai.conf","/etc/services","/etc/protocols"}
FIXED_ALIASES={"/lib":"usr/lib","/lib64":"usr/lib64"}

class Refused(Exception):pass
def need(ok,cause):
    if not ok:raise Refused(cause)

def freeze(raw,label):
    need(type(raw) is bytes and len(raw)<=16*1048576,"PRODUCER_BLOB_SIZE")
    fd=os.memfd_create(label,os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
    try:
        off=0
        while off<len(raw):
            n=os.write(fd,raw[off:]);need(n>0,"PRODUCER_WRITE");off+=n
        fcntl.fcntl(fd,fcntl.F_ADD_SEALS,SEALS)
        return fd
    except BaseException:os.close(fd);raise

class PreparedView:
    """An actual producer/consumer capability; success here is NOT admission.
    All effects below belong to a separately scoped future root admission.
    root is exclusively a NEW owned ephemeral build view; /var/tmp data mounts
    expose original custody inputs without changing them. Failure stops the owner
    process; it never recursively unmounts, deletes or repairs a foreign view.
    """
    def __init__(self,raw_manifest,manifest_sha,supervisor_source,supervisor_sha,contract_source,contract_sha, *, deadline,
                 control_case=None,control_source=None,control_sha=None):
        need(os.getuid()==os.getgid()==0,"PRODUCER_ROOT_REQUIRED")
        need(type(deadline) is int and time.monotonic_ns()<deadline<=time.monotonic_ns()+300*10**9,"PRODUCER_DEADLINE")
        self.end=deadline;self.rootfd=None;self.indexfd=None;self.closure=None;self.ready=False;self.lib=ctypes.CDLL(None,use_errno=True)
        self.control_case=control_case;self.control=None;self.control_delta=None;self.raw_manifest=raw_manifest
        self.original_manifest_sha=manifest_sha
        self.index_DATA_override=False
        need(hashlib.sha256(raw_manifest).hexdigest()==manifest_sha and hashlib.sha256(supervisor_source).hexdigest()==supervisor_sha and
             hashlib.sha256(contract_source).hexdigest()==contract_sha,"PRODUCER_EXTERNAL_INPUT_PINS")
        s=types.ModuleType("a061_approved_runtime_parser");s.__file__="/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071-SUPERVISOR.py"
        exec(compile(supervisor_source,s.__file__,"exec"),s.__dict__)
        c=types.ModuleType("a061_approved_image_codec");c.__file__="/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071-CONTRACT.py"
        exec(compile(contract_source,c.__file__,"exec"),c.__dict__)
        self.s,self.c=s,c
        doc=s.inert(raw_manifest)
        need(type(doc) is dict and set(doc)=={"schema","runtime","os_files","aliases"} and doc["schema"]=="friday.a061.runtime-image-input.v1","PRODUCER_SCHEMA")
        need(type(doc["os_files"]) is dict and {"/etc/nsswitch.conf","/etc/resolv.conf","/etc/hosts"}<=set(doc["os_files"])<=ALLOWED_OS,"PRODUCER_OS_CLOSURE")
        runtime_raw=json.dumps(doc["runtime"],separators=(",",":")).encode()
        self.closure=s.runtime_preflight_bytes(runtime_raw,deadline/1e9)
        holds=dict(self.closure.holds)
        for path,row in doc["os_files"].items():
            need(type(row) is dict and set(row)=={"bytes","sha256"} and type(row["bytes"]) is int and 0<=row["bytes"]<=1048576,"PRODUCER_OS_ROW")
            holds[path]=s.HeldSource(path,row["sha256"],row["bytes"],system=True,deadline=deadline/1e9)
        need(sum(os.fstat(v.fd).st_size for v in holds.values())<=128*1048576 and len(holds)<=8192,"PRODUCER_RUNTIME_BUDGET")
        # Complete alias input is explicitly reviewed. No arbitrary alias path or
        # undeclared loader fallback can point into a mutable data portal.
        aliases=dict(FIXED_ALIASES)
        loader=doc["runtime"]["loader_alias"]
        need(type(doc["aliases"]) is dict and set(doc["aliases"])=={loader["path"]},"PRODUCER_ALIAS_SET")
        target=doc["aliases"][loader["path"]]
        need(type(target) is str and os.path.normpath(os.path.join(os.path.dirname(loader["path"]),target))==loader["target"] and loader["target"] in holds,"PRODUCER_ALIAS_TARGET")
        aliases.update(doc["aliases"])
        # Record the physical directory spelling of a loader alias as well.
        # /lib64 may itself be an admitted directory alias; membership of its
        # canonical /usr/lib64 target must still enumerate the real loader link.
        index_aliases=dict(aliases)
        for p,target in doc["aliases"].items():
            for prefix,replacement in FIXED_ALIASES.items():
                if p.startswith(prefix+"/"):
                    physical="/"+replacement+p[len(prefix):]
                    need(physical not in holds and physical not in index_aliases,"PRODUCER_ALIAS_COLLISION")
                    index_aliases[physical]=target
        # Typed inert DATA is applied to the actual approved held closure only
        # after its original cache/ELF/stdlib checks. No changed member is ever
        # loaded here. Only native --held-a061 can consume this protected view,
        # and all nine changes must be refused before interpreter/loader start.
        if control_case is not None:
            allowed={"image_cache_format","image_cache_string","image_cache_resolution",
                "image_ELF_format","image_ELF_dependency","image_ELF_loader",
                "image_loader_execute_mode","image_import_zip","image_preload"}
            need(control_case in allowed and type(control_source) is bytes and
                len(control_source)<=1048576 and type(control_sha) is str and
                re.fullmatch("[0-9a-f]{64}",control_sha) and
                hashlib.sha256(control_source).hexdigest()==control_sha,
                "PRODUCER_INDEPENDENT_CONTROL_SOURCE_PIN")
            ctrl=types.ModuleType("a071_pinned_inert_guard_codec")
            ctrl.__file__="/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071-FULL-CONTROLS.py"
            exec(compile(control_source,ctrl.__file__,"exec"),ctrl.__dict__)
            self.control=ctrl
            before={p:blob.bytes(134217728,deadline/1e9) for p,blob in holds.items()}
            modes={p:(0o555 if p in ("/usr/bin/python3.14",loader["target"]) else 0o444) for p in holds}
            pins={**doc["runtime"]["files"],**doc["os_files"]}
            delta=ctrl.runtime_guard_data(control_case,before,modes,pins,index_aliases)
            self.control_delta=delta;changed=delta["path"]
            need(delta["authority"]=="DATA_ONLY_NOT_ADMITTED" and
                delta["executable_payload_created"] is False,"PRODUCER_CONTROL_DATA_NOT_AUTHORITY")
            if delta["kind"]!="REMOVE_LOADER_EXECUTE_BITS":
                class InertBlob:
                    def __init__(self,raw):self.fd=freeze(raw,"friday-a071-inert-native-refusal-data")
                    def close(self):
                        if self.fd is not None:
                            fd,self.fd=self.fd,None;os.close(fd)
                if changed in holds:holds[changed].close()
                holds[changed]=InertBlob(delta["replacement_DATA"])
                doc["runtime"]["files"][changed]={"bytes":delta["replacement_bytes"],
                                                   "sha256":delta["replacement_sha256"]}
                self.raw_manifest=json.dumps(doc,sort_keys=True,separators=(",",":")).encode()
                manifest_sha=hashlib.sha256(self.raw_manifest).hexdigest()
        # The source manifest logical interpreter/stdlib/library/cache grammar is
        # unchanged; every bytes consumer now maps those exact held descriptors.
        self.root="/var/tmp/friday-a061-runtime-owned-"+os.urandom(16).hex()
        os.mkdir(self.root,0o700)
        need(self.lib.unshare(0x00020000)==0,"PRODUCER_MOUNT_NAMESPACE")
        self.mount(None,"/",None,MS_REC|MS_PRIVATE,None)
        self.mount("tmpfs",self.root,"tmpfs",MS_NOSUID|MS_NODEV,"size=16777216,mode=0755")
        dirs={"/","/proc","/sys","/var","/var/tmp","/usr/lib64"}
        for p in set(holds)|set(aliases):
            parent=os.path.dirname(p)
            while parent!="/":dirs.add(parent);parent=os.path.dirname(parent)
        # Alias dirs replace equivalent ancestors only; no runtime member lives
        # physically beneath an alias. openat2 later resolves within this root.
        dirs-=set(aliases)
        for p in sorted(dirs-{"/"},key=lambda v:(v.count("/"),v)):
            os.mkdir(self.root+p,0o755)
        for p,blob in holds.items():
            self.guard()
            placeholder=os.open(self.root+p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400);os.close(placeholder)
            self.mount("/proc/self/fd/%d"%blob.fd,self.root+p,None,MS_BIND,None)
            # Executability is metadata of this exact held inode, not a named
            # interpreter reopen. Data/code role was separately reviewed.
            mode=0o555 if p in ("/usr/bin/python3.14",loader["target"]) else 0o444
            if self.control_delta is not None and p==self.control_delta["path"]:mode=self.control_delta["mode"]
            os.fchmod(blob.fd,mode)
            self.mount(None,self.root+p,None,MS_BIND|MS_REMOUNT|MS_RDONLY|MS_NOSUID|MS_NODEV,None)
        for p,target in aliases.items():
            need(type(target) is str and 0<len(target)<=511 and "\0" not in target,"PRODUCER_ALIAS_GRAMMAR")
            os.symlink(target,self.root+p)
        self.rootfd=os.open(self.root,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        class Attr(ctypes.Structure):_fields_=[("attr_set",ctypes.c_uint64),("attr_clr",ctypes.c_uint64),("propagation",ctypes.c_uint64),("userns_fd",ctypes.c_uint64)]
        attr=Attr(MOUNT_ATTR_RDONLY|MOUNT_ATTR_NOSUID|MOUNT_ATTR_NODEV,0,0,0)
        need(self.lib.syscall(442,self.rootfd,ctypes.c_char_p(b""),AT_EMPTY_PATH|0x8000,ctypes.byref(attr),ctypes.sizeof(attr))==0,"PRODUCER_RECURSIVE_IMMUTABILITY")
        # Mutable data and kernel observations are separate fixed portals. They
        # cannot contribute library/import/loader members or image authority.
        for portal in ("/proc","/sys","/var/tmp"):
            self.mount(portal,self.root+portal,None,MS_BIND|MS_REC,None)
            if portal!="/var/tmp":self.mount(None,self.root+portal,None,MS_BIND|MS_REMOUNT|MS_RDONLY|MS_NOSUID|MS_NODEV|MS_NOEXEC,None)
        rows=[];total=0
        for p in sorted(set(holds)|dirs|set(index_aliases)):
            self.guard()
            # Resolve every alias IN the protected root; an absolute loader link
            # must never be stat/hash-followed into the host OS namespace.
            class How(ctypes.Structure):_fields_=[("flags",ctypes.c_uint64),("mode",ctypes.c_uint64),("resolve",ctypes.c_uint64)]
            how=How(os.O_PATH|os.O_CLOEXEC,0,0x10|0x02)
            fd=self.lib.syscall(437,self.rootfd,ctypes.c_char_p(p.encode()),ctypes.byref(how),ctypes.sizeof(how))
            need(fd>=0,"PRODUCER_IMAGE_RESOLVE_IN_ROOT")
            try:st=os.fstat(fd)
            finally:os.close(fd)
            if p in holds:
                kind=1;before=os.fstat(holds[p].fd)
                need((st.st_dev,st.st_ino)==(before.st_dev,before.st_ino),"PRODUCER_SAME_HELD_INODE")
                size=st.st_size;sha=bytes.fromhex(doc["runtime"]["files"].get(p,doc["os_files"].get(p))["sha256"]);total+=size;target=""
            elif p in index_aliases:kind=3;size=0;sha=b"\0"*32;target=index_aliases[p]
            else:kind=4 if p in ("/proc","/sys","/var/tmp") else 2;size=0;sha=b"\0"*32;target=""
            rows.append(c.MEMBER.pack(kind,stat.S_IMODE(st.st_mode),size,st.st_dev,st.st_ino,sha,p.encode().ljust(512,b"\0"),target.encode().ljust(512,b"\0")))
        need(len(rows)<=8192 and total<=128*1048576,"PRODUCER_IMAGE_BUDGET")
        index=c.IMAGE.pack(b"FRA061I1",1,len(rows),total,bytes.fromhex(manifest_sha))+b"".join(rows)
        c.decode_image(index,manifest_sha)
        self.indexfd=freeze(index,"friday-a061-exact-runtime-image")
        self.image_sha=hashlib.sha256(index).hexdigest();self.manifest_sha=manifest_sha
        self.index_bytes=index
        self.index_view_bytes=index
        # All mounts keep kernel references to those exact sealed memfd inodes.
        # Closing originals cannot replace any loader/import member.
        for blob in holds.values():blob.close()
        self.closure.holds={};self.ready=True

    def guard(self):need(time.monotonic_ns()<self.end,"PRODUCER_DEADLINE")
    def mount(self,source,target,fs,flags,data):
        self.guard()
        cv=lambda x:None if x is None else ctypes.c_char_p(x.encode())
        need(self.lib.mount(cv(source),cv(target),cv(fs),ctypes.c_ulong(flags),cv(data))==0,"PRODUCER_MOUNT:%d"%ctypes.get_errno())

    def observations(self):
        need(self.ready,"PRODUCER_NOT_COMPLETE")
        st=os.fstat(self.rootfd)
        return {"schema":"friday.a061.unapproved-runtime-observation.v1","runtime_index_sha256":self.image_sha,
            "runtime_manifest_sha256":self.manifest_sha,"root_identity":[st.st_dev,st.st_ino],
            "mount_ns":os.stat("/proc/self/ns/mnt").st_ino,"authority":"NOT_PROVEN",
            "root_capsule_created":False,"native_execution_admitted":False,
            "control_case":self.control_case,"typed_DATA_changed":self.control_delta is not None,
            "runtime_manifest_bytes":len(self.raw_manifest),"index_DATA_override":self.index_DATA_override}

    def replace_control_index(self,case,control_source,control_sha):
        """Eight index-only DATA causes in this same already protected view.
        Replacing this owned index descriptor cannot alter runtime view bytes.
        A fresh external capsule must independently bind the new index SHA.
        """
        self.guard();need(self.ready and self.control_case is None and
            case in {"image_header","image_member_count","image_member_order",
                "image_complete_membership","image_inode","image_file_mode",
                "image_alias_target","image_parent_membership"} and
            type(control_source) is bytes and len(control_source)<=1048576 and
            hashlib.sha256(control_source).hexdigest()==control_sha,
            "PRODUCER_INDEX_CONTROL_ADMISSION")
        ctrl=types.ModuleType("a071_pinned_index_DATA_codec")
        ctrl.__file__="/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071-FULL-CONTROLS.py"
        exec(compile(control_source,ctrl.__file__,"exec"),ctrl.__dict__)
        delta=ctrl.guard_data_request(case,image_bytes=self.index_bytes)
        replacement=freeze(delta["replacement_index_DATA"],"friday-a071-index-refusal-data")
        old,self.indexfd=self.indexfd,replacement;os.close(old)
        self.control_case=case;self.control=ctrl;self.control_delta=delta
        self.index_bytes=delta["replacement_index_DATA"]
        self.image_sha=delta["replacement_sha256"];self.index_DATA_override=True

    def check_protected_members(self):
        """Observed bytes/metadata of every actual protected-view member.
        Uses the original complete view index even for an index-DATA negative.
        No call to a loader, interpreter, native verifier or privileged service.
        """
        self.guard();need(self.ready,"PRODUCER_NOT_COMPLETE")
        _,_,count,total,_=self.c.IMAGE.unpack_from(self.index_view_bytes)
        rows=[self.c.MEMBER.unpack_from(self.index_view_bytes,56+1088*i) for i in range(count)]
        paths={r[6].split(b"\0",1)[0].decode("ascii") for r in rows}
        result=[]
        class How(ctypes.Structure):
            _fields_=[("flags",ctypes.c_uint64),("mode",ctypes.c_uint64),("resolve",ctypes.c_uint64)]
        for i in range(count):
            self.guard()
            kind,mode,size,dev,ino,sha,path,target=rows[i]
            logical=path.split(b"\0",1)[0];how=How(os.O_RDONLY|os.O_CLOEXEC|(0 if kind==3 else os.O_NOFOLLOW),0,0x10|0x02)
            fd=self.lib.syscall(437,self.rootfd,ctypes.c_char_p(logical),ctypes.byref(how),ctypes.sizeof(how))
            need(fd>=0,"PRODUCER_CONTROL_VIEW_OPEN")
            try:
                st=os.fstat(fd)
                need((st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode))==(dev,ino,mode),
                    "PRODUCER_CONTROL_VIEW_IDENTITY")
                observed=None
                if kind==1:
                    need(st.st_uid==st.st_gid==0 and st.st_size==size and
                         fcntl.fcntl(fd,fcntl.F_GET_SEALS)&SEALS==SEALS,"PRODUCER_CONTROL_MEMBER_SEALS")
                    h=hashlib.sha256();off=0
                    while off<size:
                        self.guard();raw=os.pread(fd,min(65536,size-off),off)
                        need(raw,"PRODUCER_CONTROL_MEMBER_READ");h.update(raw);off+=len(raw)
                    observed=h.hexdigest();need(observed==sha.hex(),"PRODUCER_CONTROL_MEMBER_PIN")
                elif kind==2:
                    parent=logical.decode("ascii")
                    expected=sorted(os.path.basename(p) for p in paths if p!="/" and os.path.dirname(p)==parent)
                    need(sorted(os.listdir(fd))==expected,"PRODUCER_CONTROL_COMPLETE_DIRECTORY_MEMBERSHIP")
                result.append({"path":logical.decode("ascii"),"kind":kind,"mode":mode,
                    "bytes":size,"dev":dev,"ino":ino,"sha256":observed})
            finally:os.close(fd)
        return result

    def finish(self,capsule_fd,capsule_sha,source_fds,outer_group,inner_group):
        """Connect this actual image to the exact static public consumer.
        Caller supplies independent root capsule/pins; no status/hash is minted.
        Native pre-interpreter validates ALL actual kernel/OS/source values again.
        """
        self.guard();need(self.ready and set(source_fds)==set(self.c.SOURCE_FDS),"PRODUCER_SOURCE_DESCRIPTOR_SET")
        cap=self.c.decode_capsule(self.c.held(capsule_fd,capsule_sha,self.c.CAP.size),capsule_sha)
        st=os.fstat(self.rootfd)
        need(cap["image_sha"]==self.image_sha and cap["manifest_sha"]==self.manifest_sha and cap["root_identity"]==(st.st_dev,st.st_ino) and
             cap["mount_ns"]==os.stat("/proc/self/ns/mnt").st_ino,"PRODUCER_CONSUMER_CORRESPONDENCE")
        mappings={100:capsule_fd,111:self.indexfd,120:outer_group,121:inner_group,122:self.rootfd,**source_fds}
        copies={number:fcntl.fcntl(fd,fcntl.F_DUPFD_CLOEXEC,256) for number,fd in mappings.items()}
        try:
            for number,fd in copies.items():os.dup2(fd,number,inheritable=True)
        finally:
            for fd in copies.values():os.close(fd)
        keep={0,1,2}|set(mappings)
        for n in os.listdir("/proc/self/fd"):
            fd=int(n)
            if fd not in keep:
                try:os.close(fd)
                except OSError:pass
        os.execve(110,["friday-a061-static-owned","--held-a061",capsule_sha],self.c.ENV)
