"""Actual Root-created native children, held exec and direct wait4/reap.

The performer may request only a declared semantic verb. It never supplies a
tool, PID, pidfd, pipe, shell text or arbitrary argv. Tools and profiles come
from the independently qualified enrollment/admission.
"""
import ctypes
import contextlib
import errno
import os
import resource
import selectors
import signal
import stat
from common import (Refused, INPUT_MAX, OUTPUT_MAX, DOCUMENT_MAX, WORKERS_MAX,
                    integer, text, exact, mono, error_fact, sha, OwnedPreimage)
from custody import Held, open_absolute, identity9
from lifetime import (OwnedFDs, bounded_direct_reap, ForkOwner, receive_fork_owner,
    stock_inherited_upper, FD_HISTORY_ALLOCATION)

CLONE_NEWNS = 0x00020000
CLONE_NEWNET = 0x40000000
MS_RDONLY, MS_BIND, MS_REC, MS_PRIVATE, MS_REMOUNT = 1,4096,16384,1<<18,32
MS_NOSUID,MS_NODEV,MS_NOEXEC=2,4,8
PR_SET_PDEATHSIG, PR_SET_CHILD_SUBREAPER = 1,36
libc = ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes = [ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
libc.mount.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_void_p]
libc.unshare.argtypes = [ctypes.c_int]


def checked(result, cause):
    if result != 0:
        e = ctypes.get_errno()
        raise OSError(e,os.strerror(e),cause)


def child_guard(parent_pid):
    checked(libc.prctl(PR_SET_PDEATHSIG,signal.SIGKILL,0,0,0),"pdeathsig")
    if os.getppid() != parent_pid: os._exit(125)


def _child_journal_close(book, fd):
    if type(fd) is not int or fd < 0:
        return
    book.close_one(fd)
    if fd in book.fds:
        meta = book.meta.get(fd, {})
        raise Refused("FD_CLOSE_UNCONFIRMED", "terminal", {"fd": fd, "holder": meta.get("holder"),
            "credit": meta.get("credit"), "identity9_decimal_strings": meta.get("identity9_decimal_strings"),
            "status": meta.get("status")})


def sandbox(image, input_mounts, uid, gid, credit):
    """Real private mount/network namespaces; no host install or global mount."""
    if os.geteuid() != 0: raise Refused("qualified_native_namespace_unavailable")
    checked(libc.unshare(CLONE_NEWNS|CLONE_NEWNET),"private-namespaces")
    checked(libc.mount(None,b"/",None,MS_REC|MS_PRIVATE,None),"private-mounts")
    root = image["path"]
    checked(libc.mount(root.encode(),root.encode(),None,MS_BIND|MS_REC,None),"image-bind")
    checked(libc.mount(None,root.encode(),None,MS_BIND|MS_REMOUNT|MS_RDONLY|MS_REC,None),"image-read-only")
    for held,target in input_mounts:
        held.check()
        # Target placeholders are part of the exact approved image inventory.
        if target not in image["input_placeholders"]: raise Refused("image_input_placeholder")
        actual = root+target
        book = OwnedFDs(credit=credit)
        fd = open_absolute(actual,journal=book)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise Refused("image_input_placeholder")
        finally:
            _child_journal_close(book, fd)
        checked(libc.mount(("/proc/self/fd/"+str(held.fd)).encode(),actual.encode(),None,MS_BIND,None),"same-held-input-bind")
        checked(libc.mount(None,actual.encode(),None,MS_BIND|MS_REMOUNT|MS_RDONLY,None),"same-held-input-read-only")
    book = OwnedFDs(credit=credit)
    rootfd = open_absolute(root,os.O_RDONLY|os.O_DIRECTORY,journal=book)
    try:
        os.fchdir(rootfd);os.chroot(".");os.chdir("/inputs/native-job")
    finally:
        _child_journal_close(book, rootfd)
    os.setgroups([]);os.setgid(gid);os.setuid(uid)


def actor_sandbox(image,readonly_roots,output_root,credit):
    """Root-owned actor gets the selected image, immutable input trees and job.

    Only the private output tree is writable. No host package installation or
    global mount is performed; no actor-root/caller-supplied authority is used.
    """
    if os.geteuid()!=0:raise Refused("qualified_actor_namespace_unavailable")
    checked(libc.unshare(CLONE_NEWNS|CLONE_NEWNET),"actor-private-namespaces")
    checked(libc.mount(None,b"/",None,MS_REC|MS_PRIVATE,None),"actor-private-mounts")
    root=image["path"]
    checked(libc.mount(root.encode(),root.encode(),None,MS_BIND|MS_REC,None),"actor-image-bind")
    for selected in readonly_roots:
        book=OwnedFDs(credit=credit)
        fd=open_absolute(selected["path"],os.O_RDONLY|os.O_DIRECTORY,journal=book)
        try:
            if identity9(os.fstat(fd))!=selected["identity9_decimal_strings"]:raise Refused("actor_root_custody")
            target=root+selected["path"]
            targetfd=open_absolute(target,os.O_RDONLY|os.O_DIRECTORY,journal=book)
            _child_journal_close(book, targetfd)
            checked(libc.mount(("/proc/self/fd/"+str(fd)).encode(),target.encode(),None,MS_BIND|MS_REC,None),"actor-held-root-bind")
            checked(libc.mount(None,target.encode(),None,MS_BIND|MS_REMOUNT|MS_RDONLY|MS_NOSUID|MS_NODEV,None),"actor-readonly-root")
        finally:_child_journal_close(book, fd)
    target=root+output_root
    outbook=OwnedFDs(credit=credit)
    targetfd=open_absolute(target,os.O_RDONLY|os.O_DIRECTORY,journal=outbook)
    _child_journal_close(outbook, targetfd)
    checked(libc.mount(output_root.encode(),target.encode(),None,MS_BIND|MS_REC,None),"actor-private-output-bind")
    # /proc is needed solely for inherited sealed/held executable/source FDs.
    # It is mounted inside this private image; the Root parent observes actual
    # process/pidfd identities from its own namespace, never supplied labels.
    checked(libc.mount(b"proc",(root+"/proc").encode(),b"proc",MS_NOSUID|MS_NODEV|MS_NOEXEC,None),"actor-proc")
    checked(libc.mount(None,root.encode(),None,MS_BIND|MS_REMOUNT|MS_RDONLY,None),"actor-image-readonly")
    rootbook=OwnedFDs(credit=credit)
    fd=open_absolute(root,os.O_RDONLY|os.O_DIRECTORY,journal=rootbook)
    try:os.fchdir(fd);os.chroot(".");os.chdir(output_root)
    finally:_child_journal_close(rootbook, fd)


class RootNative:
    def __init__(self, observer, output_store, enrollment):
        self.observer, self.store, self.enrollment = observer,output_store,enrollment
        self.admission = None
        self.invocations = []
        self.unconfirmed={}
        self.capture_owners={}
        checked(libc.prctl(PR_SET_CHILD_SUBREAPER,1,0,0,0),"subreaper")

    def qualify(self, admission):
        self.admission = admission

    def _run(self, tool_pin, argv, env, profile, image=None, mounts=(),dependencies=()):
        # This is performing native execution, not a builder or external stub.
        exact(profile,("read_max","stdout_max","stderr_max","wall_ns","allocation_max","slots"))
        for k in ("read_max","stdout_max","stderr_max","wall_ns","allocation_max","slots"):
            integer(profile[k],10**21)
        if profile["stdout_max"]+profile["stderr_max"] > INPUT_MAX or profile["slots"] != 1:
            raise Refused("native_profile")
        if len(self.observer.processes)>=128:raise Refused("process_history_before_fork")
        # Returned terminal/invocation metadata escapes the native call and is
        # retained by the same Root. Capture bytearrays also coexist with that
        # construction after the primary native hold is retired. Admit and own
        # this complete overlap BEFORE any allocation/fork; do not retire it
        # while invocation/returned metadata aliases still exist.
        strings=sum(len(a) for a in argv)+sum(len(k)+len(v) for k,v in env.items())
        metadata_hold=self.observer.reserve("native-returned-invocation-lifetime-before-effect",
            allocation=131072+128*8192+strings*8+len(argv)*1024+len(dependencies)*4096+
                2*(profile["stdout_max"]+profile["stderr_max"]))
        metadata_owner={"invocation":None,"terminal":None}
        self.observer.own_result(metadata_owner,metadata_hold)
        child_owner_hold=self.observer.reserve("native-child-complete-owner-before-fork",
            reads=2*(INPUT_MAX+8),output=2*(INPUT_MAX+8),allocation=FD_HISTORY_ALLOCATION+INPUT_MAX*1120+131072,
            slots=stock_inherited_upper(self.observer)+4)
        child_dup_hold=self.observer.reserve("native-child-stdio-cells-before-fork",allocation=131072,slots=2)
        metadata_owner["child_owner_hold"]=child_owner_hold
        metadata_owner["child_dup_hold"]=child_dup_hold
        self.observer.own_result(metadata_owner,child_owner_hold)
        child_owner=None;child_r=child_w=-1;child_packet=None
        hold = self.observer.reserve("native-known-before-effect",
            reads=profile["read_max"]+profile["stdout_max"]+profile["stderr_max"],output=2*(profile["stdout_max"]+profile["stderr_max"]),
            allocation=profile["allocation_max"]+2*(profile["stdout_max"]+profile["stderr_max"])+65536,slots=8,
            hash_bytes=2*(profile["stdout_max"]+profile["stderr_max"]))
        pid = pidfd = -1
        out_r=out_w=err_r=err_w=gate_r=gate_w=-1
        raw_out,raw_err,raw_exec = bytearray(),bytearray(),None
        first_error=None;tool=None;dependency_leases=[];fds=OwnedFDs(credit=hold);invocation=None
        self.observer.retain_local_owner(fds)
        self.capture_owners=getattr(self,"capture_owners",{})
        status,usage,finished,io_fault,cleanup = None,None,None,None,[]
        started = mono()
        suffix=str(started)+"-"+str(len(self.invocations))
        capture_names=("native-"+suffix+"-stdout","native-"+suffix+"-stderr")
        capture={"stdout":None,"stderr":None,"digests":{},"preimages":{},
            "retention_confirmed":False,"capture_names":capture_names,"hold":hold,
            "fds":fds,"pid":pid,"raw_stdout":raw_out,"raw_stderr":raw_err,
            "metadata_owner":metadata_owner,"metadata_hold":metadata_hold}
        self.capture_owners[suffix]=capture
        metadata_owner["capture"]=capture
        deadline = min(self.observer.reserve_deadline,started+profile["wall_ns"])
        try:
            # Both complete capture FD lifetimes exist before tool/native
            # effects. Terminal writes consume only their live prepaid leases.
            for name in capture_names:self.store.prepare_reserved(name)
            tool=Held(tool_pin["path"],tool_pin,self.observer,DOCUMENT_MAX).__enter__()
            for pin in dependencies:
                dependency_leases.append(Held(pin["path"],pin,self.observer,DOCUMENT_MAX).__enter__())
            with contextlib.nullcontext():
                # Pipes and child are constructed by the actual Root supervisor.
                out_r,out_w = fds.pair(os.O_CLOEXEC|os.O_NONBLOCK)
                err_r,err_w = fds.pair(os.O_CLOEXEC|os.O_NONBLOCK)
                gate_r,gate_w = fds.pair(os.O_CLOEXEC)
                fds.grant(child_owner_hold)
                child_r,child_w=fds.pair(os.O_CLOEXEC,credit=child_owner_hold)
                child_owner=ForkOwner(self.observer,child_owner_hold)
                metadata_owner["child_owner"]=child_owner
                pid = os.fork()
                if pid == 0:
                    try:
                        child_owner.adopt()
                        child_guard(self.observer.owner_pid)
                        for fd in (out_r,err_r,gate_w,child_r):child_owner.close_one(fd)
                        dupbook=OwnedFDs(credit=child_dup_hold)
                        child_owner.close_one(1);dupbook.acquire(os.dup2,out_w,1,holder="child-stdout")
                        child_owner.close_one(2);dupbook.acquire(os.dup2,err_w,2,holder="child-stderr")
                        os.set_blocking(1,True);os.set_blocking(2,True)
                        resource.setrlimit(resource.RLIMIT_FSIZE,(profile["stdout_max"]+profile["stderr_max"],)*2)
                        resource.setrlimit(resource.RLIMIT_AS,(profile["allocation_max"],)*2)
                        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
                        resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
                        # Child cannot reach tool effects until Root records the
                        # actual pidfd/cgroup/pipe acquisition and releases gate.
                        if os.read(gate_r,1) != b"G": os._exit(126)
                        tool.check()
                        for lease in dependency_leases:lease.check()
                        if image is not None:
                            sandbox(image,mounts,tool_pin["run_uid"],tool_pin["run_gid"],hold)
                            child_guard(self.observer.owner_pid)
                        keep = {0,1,2,tool.fd,gate_r,child_w}|{held.fd for held,_ in mounts}
                        for held,_ in mounts:
                            child_owner.keep(held.fd)
                        # Snapshot actual descriptors before chroot when /proc
                        # is unavailable; all inherited Root FDs must retire.
                        for fd in tuple(child_owner.fds):
                            if fd not in keep:
                                child_owner.close_one(fd)
                        child_owner.emit(child_w)
                        if os.read(gate_r,1)!=b"A":raise Refused("child_owner_not_accepted","terminal")
                        if self.observer.fd_state.unknown():raise Refused("FD_CLOSE_UNCONFIRMED","terminal")
                        # Linux execve(fd) uses the SAME held executable inode.
                        os.execve(tool.fd,argv,env)
                    except BaseException as exc:
                        try:
                            child_owner.emit_failure(child_w,exc)
                        except BaseException:pass
                        raw = (type(exc).__name__+":"+str(exc)).encode("utf-8","backslashreplace")
                        try: os.write(2,raw[:profile["stderr_max"]])
                        except BaseException: pass
                        os._exit(127)
                pidfd = fds.acquire(os.pidfd_open,pid,0,credit=hold,holder="native-pidfd")
                fds.close_one(out_w);out_w=-1;fds.close_one(err_w);err_w=-1;fds.close_one(gate_r);gate_r=-1
                fds.close_one(child_w);child_w=-1
                # Root provisioned empty cgroup is held independently; no path
                # supplied by the performer can choose its budget/ownership.
                cfd = open_absolute(self.observer.cgroup+"/cgroup.procs",os.O_WRONLY,journal=fds)
                try: os.write(cfd,str(pid).encode("ascii"))
                finally:
                    fds.close_one(cfd)
                    if cfd in fds.fds:
                        meta=fds.meta.get(cfd, {})
                        raise Refused("FD_CLOSE_UNCONFIRMED","terminal",{"fd":cfd,"holder":meta.get("holder"),"credit":meta.get("credit"),"identity9_decimal_strings":meta.get("identity9_decimal_strings"),"status":meta.get("status")})
                self.observer.register(pid,pidfd)
                invocation={"pid":pid,"pidfd_identity9":identity9(os.fstat(pidfd)),
                    "tool_pin":tool.pin_now(),"argv":list(argv),"environment":dict(env),
                    "stdout_pipe":identity9(os.fstat(out_r)),"stderr_pipe":identity9(os.fstat(err_r)),
                    "started_ns":started,"before_effect_profile":dict(profile)}
                self.invocations.append(invocation)
                os.write(gate_w,b"G")
                child_packet=receive_fork_owner(child_r,child_owner_hold,deadline,pid,self.observer.owner_pid)
                invocation["child_owner_graph"]=child_packet
                metadata_owner["accepted_child_owner_graph"]=child_packet
                if child_packet.get('schema')=='friday.a190.fork-final-failure.v1':
                    metadata_owner['actual_child_final_failure']=child_packet
                    raise Refused('native_child_bootstrap_failure','terminal',child_packet)
                os.write(gate_w,b"A");fds.close_one(gate_w);gate_w=-1
                with selectors.PollSelector() as sel:
                    sel.register(out_r,selectors.EVENT_READ,("stdout",raw_out,profile["stdout_max"]))
                    sel.register(err_r,selectors.EVENT_READ,("stderr",raw_err,profile["stderr_max"]))
                    sel.register(pidfd,selectors.EVENT_READ,("pidfd",None,None))
                    sel.register(child_r,selectors.EVENT_READ,('owner-final-failure',None,None))
                    terminal_seen = False
                    while sel.get_map():
                        left = (deadline-mono())/1e9
                        if left <= 0: raise Refused("native_deadline","execution")
                        self.observer.check()
                        ready = sel.select(min(left,0.1))
                        for key,_ in ready:
                            name,target,maximum = key.data
                            if name=='owner-final-failure':
                                packet=receive_fork_owner(child_r,child_owner_hold,deadline,pid,self.observer.owner_pid,
                                    final_failure=True,allow_eof=True)
                                metadata_owner['actual_child_final_failure']=packet
                                if packet is not None:
                                    invocation['actual_child_final_failure']=packet
                                    invocation['child_actual_failure_graph_complete']=packet.get('complete') is True
                                sel.unregister(child_r)
                                continue
                            if name == "pidfd":
                                terminal_seen=True;finished=mono()
                                try: self.observer.before_reap(pid)
                                except BaseException as exc: io_fault=exc
                                sel.unregister(pidfd)
                                continue
                            try: part = os.read(key.fd,min(65536,maximum-len(target)+1))
                            except BlockingIOError: continue
                            if not part: sel.unregister(key.fd);continue
                            if len(target)+len(part) > maximum: raise Refused("native_stream_cap","execution")
                            target.extend(part);hold.commit(output=len(part))
                        if terminal_seen and not any(k.data[0]!="pidfd" for k in sel.get_map().values()):
                            break
                status,usage,reap_faults=bounded_direct_reap(self.observer,pid,pidfd)
                cleanup.extend(reap_faults)
                if io_fault is not None: raise io_fault
                tool.check()
        except BaseException as exc:
                first_error=exc
                raw_exec = error_fact(exc,"execution")
                self.observer.errors.append(raw_exec)
                self.observer.note_partial("native-stdout-"+str(pid),len(raw_out))
                self.observer.note_partial("native-stderr-"+str(pid),len(raw_err))
        finally:
                if pid > 0 and status is None:
                    try:
                        if pidfd >= 0: signal.pidfd_send_signal(pidfd,signal.SIGKILL,None,0)
                        else:
                            pidfd=fds.acquire(os.pidfd_open,pid,0,credit=hold,holder="native-pidfd")
                            signal.pidfd_send_signal(pidfd,signal.SIGKILL,None,0)
                    except ProcessLookupError: pass
                    except BaseException as exc: cleanup.append(error_fact(exc,"terminal"))
                    try:
                        status,usage,reap_faults=bounded_direct_reap(self.observer,pid,pidfd,kill=True)
                        cleanup.extend(reap_faults)
                    except BaseException as exc: cleanup.append(error_fact(exc,"terminal"))
                if status is None and pid>0:
                    leases=[tool] if tool is not None else []
                    leases.extend(held for held,target in mounts)
                    leases.extend(dependency_leases)
                    for lease in leases:lease.retain_until_terminal(pid)
                    if pidfd>=0:self.observer.pidfds[pid]=pidfd
                    self.unconfirmed[pid]={"pidfd":pidfd,"fds":fds,"leases":leases,
                        "hold":hold,"stdout":raw_out,"stderr":raw_err,"invocation":invocation}
                    self.observer.note_cleanup({"cause":"STOP_UNCONFIRMED","pid":pid,
                        "reservation":hold.token,"full_exec_input_pipe_FD_graph_retained":True,
                        "existing_Root_owner_pid":self.observer.owner_pid})
                else:
                    cleanup.extend(fds.close());self.observer.pidfds.pop(pid,None)
                if tool is not None and (status is not None or pid<=0):
                    try:tool.check()
                    except BaseException as exc:cleanup.append(error_fact(exc,"terminal"))
                    cleanup.extend(tool.retire())
                if status is not None or pid<=0:
                    for lease in dependency_leases:cleanup.extend(lease.retire())
                    leases=([tool] if tool is not None else [])+dependency_leases+[held for held,target in mounts]
                    if fds.fds or any(held.fd>=0 for held in leases if held not in [h for h,t in mounts]):
                        for lease in leases:
                            if lease.fd>=0:lease.retain_until_terminal(pid)
                        self.unconfirmed[pid]={"pidfd":pidfd,"fds":fds,"leases":leases,
                            "hold":hold,"stdout":raw_out,"stderr":raw_err,"invocation":invocation,
                            "status":"FD_CLOSE_UNCONFIRMED","confirmed_wait_status":status}
                        cleanup.append({"cause":"FD_CLOSE_UNCONFIRMED","pid":pid,"full_graph_retained":True})
                if cleanup: self.observer.cleanup_faults.extend(cleanup)
                if status is not None or pid<=0:
                    # wait4 confirms the child table lifetime, not the attempted
                    # close facts; preserve those in the accepted packet.
                    if child_owner is not None:
                        for slot,row in list(getattr(child_owner_hold,'fd_rows',{}).items()):
                            if row.get('journal') is child_owner:child_owner_hold.fd_rows.pop(slot)
                    child_owner_hold.retire_slots();child_dup_hold.release()
                    if child_packet is not None:
                        packets=[child_packet]
                        final_packet=metadata_owner.get('actual_child_final_failure')
                        if final_packet is not None and final_packet is not child_packet:packets.append(final_packet)
                        self.observer.retain_allocation(child_owner_hold.token,
                            sum(packet['_receiver_resident_upper'] for packet in packets)+128*8192)
        # Full stream preimages remain regular held files for A128 consumers.
        capture["pid"]=pid
        if pid in self.unconfirmed:self.unconfirmed[pid]["capture"]=capture
        try:
            capture["stdout"]=bytes(raw_out);capture["stderr"]=bytes(raw_err)
            for stream in ("stdout","stderr"):
                preimage=OwnedPreimage(capture[stream],self.observer,hold)
                capture["preimages"][stream]=preimage
                capture["digests"][stream]=preimage.for_same_object(capture[stream])
            out=self.store.put_reserved(capture_names[0],capture["stdout"],"native","native/"+suffix+"/stdout",hold,capture["preimages"]["stdout"])
            err=self.store.put_reserved(capture_names[1],capture["stderr"],"native","native/"+suffix+"/stderr",hold,capture["preimages"]["stderr"])
            capture["retention_confirmed"]=True
        except BaseException as retention_error:
            self.observer.errors.append(error_fact(retention_error,"terminal"))
            self.observer.retain_error_arena(retention_error)
            if status is not None or pid<=0:
                # A real same-Root receiver takes the full existing objects and
                # metadata credit; no new reserve/write/hash after failure.
                self.accept_failed_capture(suffix,status)
            if first_error is not None:raise first_error
            raise
        finally:
            if pid not in self.unconfirmed and capture["retention_confirmed"]:
                if not fds.fds and hold.release() is True:
                    self.capture_owners.pop(suffix,None)
                    capture['stdout']=capture['stderr']=None;capture['preimages'].clear()
                    raw_out.clear();raw_err.clear();metadata_owner['capture']=None
                    self.observer.retire_local_owner(fds)
                else:
                    self.observer.note_cleanup({'cause':'native_capture_credit_release_unconfirmed','token':hold.token})
        record = {"pid":pid,"exit_code":os.waitstatus_to_exitcode(status) if status is not None else None,
            "raw_wait_status":status,"started_ns":started,"finished_ns":finished or mono(),
            "stdout":out,"stderr":err,"raw_error":raw_exec,"cleanup":cleanup,
            "tool_pin":dict(tool_pin),"argv":list(argv),"environment":dict(env),
            "rusage":next((r["rusage"] for r in self.observer.waits if r["pid"]==pid),None)}
        if invocation is None:
            invocation={"pid":pid,"started_ns":started,"before_effect_profile":dict(profile),"before_exec_failure":True}
            self.invocations.append(invocation)
        invocation["terminal"]=record
        metadata_owner["invocation"]=invocation;metadata_owner["terminal"]=record
        if first_error is not None:raise first_error
        return record

    def accept_failed_capture(self,capture_id,status):
        self.observer._owned();capture=self.capture_owners.get(capture_id)
        if capture is None:raise Refused('capture_owner_identity','terminal')
        pid=capture['pid']
        if pid>0 and not any(r['pid']==pid and r['raw_wait_status']==status for r in self.observer.waits):
            raise Refused('capture_requires_confirmed_child','terminal')
        receipt=self.observer.accept_capture(capture_id,capture,capture['metadata_owner'],capture['metadata_hold'])
        capture['accepted_receipt']=receipt
        # Parent UNKNOWN FDs remain on their original live grant. Raw/cached/
        # metadata aliases have transferred to the actual recipient arena.
        if not capture['fds'].fds:
            receipt['ordinary_native_grant_retirement_confirmed']=capture['hold'].release() is True
            if receipt['ordinary_native_grant_retirement_confirmed']:self.observer.retire_local_owner(capture['fds'])
        self.capture_owners.pop(capture_id)
        graph=self.unconfirmed.get(pid)
        if graph is not None:
            graph['capture_accepted_receipt']=receipt;graph.pop('capture',None)
        return receipt

    def retire_confirmed(self,pid,status):
        if status is None:raise Refused("retire_unconfirmed_child","terminal")
        graph=self.unconfirmed.get(pid)
        if graph is None:return
        # Direct wait4 confirmation was already recorded by the same Root.
        if not any(r["pid"]==pid and r["raw_wait_status"]==status for r in self.observer.waits):
            raise Refused("retirement_terminal_identity","terminal")
        if "capture" in graph and not graph["capture"]["retention_confirmed"]:
            graph["confirmed_wait_status"]=status
            graph["status"]="CAPTURE_RETENTION_UNCONFIRMED"
            return
        for held in graph["leases"]:self.observer.cleanup_faults.extend(held.retire(confirmed=True))
        self.observer.cleanup_faults.extend(graph["fds"].close())
        if graph["fds"].fds or any(held.fd>=0 for held in graph["leases"]):
            graph["status"]="FD_CLOSE_UNCONFIRMED"
            self.observer.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","pid":pid,"full_graph_retained":True})
            return
        if graph['hold'].release() is not True:
            graph['status']='CREDIT_RELEASE_UNCONFIRMED';return
        graph['stdout'].clear();graph['stderr'].clear()
        self.observer.pidfds.pop(pid,None)
        self.unconfirmed.pop(pid)
        self.observer.retire_local_owner(graph["fds"])
        for name,capture in list(self.capture_owners.items()):
            if capture["pid"]==pid:self.capture_owners.pop(name)

    def ownership(self):
        # Freeze once and retain that exact immutable object + complete digest.
        # Terminal, tail and fallback reuse metadata; none creates a new bytes
        # copy or hashes a mutable buffer again. At most one extra full hash
        # pass exists here. A failed original hash stays explicitly unknown,
        # with its full immutable preimage retained; handoff never retries it.
        captures=[]
        for name,capture in self.capture_owners.items():
            captures.append({"capture_id":name,"pid":capture["pid"],
                "stdout_size":len(capture["stdout"]) if capture["stdout"] is not None else len(capture["raw_stdout"]),"stdout_sha256":capture["digests"].get("stdout"),
                "stderr_size":len(capture["stderr"]) if capture["stderr"] is not None else len(capture["raw_stderr"]),"stderr_sha256":capture["digests"].get("stderr"),
                "hash_status":"FULL_SAME_OBJECT_DIGEST_RETAINED" if set(capture["digests"])=={"stdout","stderr"} else "UNKNOWN_NOT_ZERO_NOT_PROVEN",
                "immutable_same_object_full_preimages_retained":capture["stdout"] is not None and capture["stderr"] is not None,
                "full_last_known_raw_buffers_retained":True,
                "retention_confirmed":capture["retention_confirmed"],"reservation_token":capture["hold"].token,
                "journal":capture["fds"].graph(),"same_existing_Root_owner_retained":True})
        children=[{"pid":pid,"pidfd":graph["pidfd"],"fds":sorted(graph["fds"].fds),
            "held_pins":[{"fd":held.fd,"path":held.path,"expected_pin":held.pin,"last_acquired_identity9":held.before,"raw_body_sha256":held.body_sha} for held in graph["leases"] if held.fd>=0],
            "complete_journal":graph["fds"].graph(),
            "confirmed_wait_status":graph.get("confirmed_wait_status"),
            "reservation_token":graph["hold"].token,"status":graph.get("status","STOP_UNCONFIRMED")}
            for pid,graph in self.unconfirmed.items()]
        return children+captures

    def verify_admission(self, admission, signature, key, enrollment):
        tool = enrollment["signature_tool"]
        # Fixed OpenSSL verification on same-held descriptors, no shell.
        argv = [tool["path"],"dgst","-sha256","-verify","/proc/self/fd/"+str(key.fd),
                "-signature","/proc/self/fd/"+str(signature.fd),"/proc/self/fd/"+str(admission.fd)]
        return self._run(tool,argv,{"LC_ALL":"C","LANG":"C"},
            enrollment["bootstrap_helper_profile"],mounts=((key,""),(signature,""),(admission,"")),
            dependencies=enrollment["signature_tool_dependencies"])

    def verify_selected_input(self,record,signature,key):
        if self.admission is None:raise Refused("independent_selector_not_admitted")
        tool=self.enrollment["signature_tool"]
        if key.body_sha!=self.admission["independent_selector"]["key_pin"]["sha256"]:raise Refused("independent_selector_key")
        argv=[tool["path"],"dgst","-sha256","-verify","/proc/self/fd/"+str(key.fd),
            "-signature","/proc/self/fd/"+str(signature.fd),"/proc/self/fd/"+str(record.fd)]
        return self._run(tool,argv,{"LC_ALL":"C","LANG":"C"},self.enrollment["bootstrap_helper_profile"],
            mounts=((key,""),(signature,""),(record,"")),
            dependencies=self.enrollment["signature_tool_dependencies"])

    def perform(self, operation, verb, inputs):
        if self.admission is None or operation not in self.admission["effects"]:
            raise Refused("native_not_admitted")
        spec = self.admission["tools"].get(verb)
        if spec is None or operation not in spec["operations"]:
            raise Refused("native_verb")
        # The parent independently reacquires every selected original body.
        from contextlib import ExitStack
        with ExitStack() as stack:
            selected = []
            for input_id in inputs:
                source = self.admission["inputs"].get(input_id)
                if source is None or operation not in source["operations"]: raise Refused("native_input")
                selected.append(stack.enter_context(Held(source["path"],source,self.observer,DOCUMENT_MAX)))
            if verb in ("ubuntu-gpgv","node-gpgv"):
                if len(selected) != 2: raise Refused("native_input")
                filename = "inrelease" if verb=="ubuntu-gpgv" else "shasums256"
                argv = ["/usr/bin/gpgv","--keyring",spec["keyring_target"],filename]
                mounts = ((selected[0],"/inputs/native-job/"+filename),(selected[1],spec["keyring_target"]))
            elif verb == "readelf":
                if len(selected) != 1: raise Refused("native_input")
                argv = ["/usr/bin/readelf","--wide","--file-header","--dynamic","/inputs/native-job/member"]
                mounts = ((selected[0],"/inputs/native-job/member"),)
            elif verb=="retained-signature":
                if len(selected)!=3:raise Refused("native_input")
                # The verification key is independently enrolled under the
                # qualified Root admission, not chosen/minted by the performer.
                if self.admission["inputs"][inputs[2]]["sha256"]!=spec["verification_key_sha256"]:
                    raise Refused("retained_verification_key")
                argv=[spec["pin"]["path"],"dgst","-sha256","-verify","/inputs/native-job/key",
                    "-signature","/inputs/native-job/signature","/inputs/native-job/record"]
                mounts=((selected[0],"/inputs/native-job/record"),(selected[1],"/inputs/native-job/signature"),
                        (selected[2],"/inputs/native-job/key"))
            elif verb in ("python-version","node-version","browser-version","loader-list"):
                if inputs: raise Refused("native_input")
                argv = list(spec["fixed_argv"])
                mounts = ()
                if any(x in ("--no-sandbox","-c","-m","--eval") for x in argv): raise Refused("native_scope")
            else:
                raise Refused("native_verb")
            return self._run(spec["pin"],argv,{"LC_ALL":"C","LANG":"C"},
                             spec["profile"],self.admission["image"],mounts,spec["dependencies"])
