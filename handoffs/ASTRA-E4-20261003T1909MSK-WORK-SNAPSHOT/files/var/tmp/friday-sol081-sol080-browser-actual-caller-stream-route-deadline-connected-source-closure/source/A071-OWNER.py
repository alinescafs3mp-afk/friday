"""Production and full benign entry share this actual owned worker consumer.
No numeric PID adoption, callback launcher, or mock native implementation.
"""
import ctypes
import fcntl
import json
import os
import select
import time
import a061_contract as c

class NativeOwner:
    def __init__(self,executor,cap,library):
        self.x,self.cap,self.lib=executor,cap,library
        self.gate=(-1,-1)
        self.gate_released=False
        if cap["mode_id"]==4:
            self.gate=os.pipe2(os.O_CLOEXEC|os.O_NONBLOCK)

    def launch(self,entry,out,context,run,worker,scheduler_end):
        x=self.x;run.guard("launch")
        x.need(worker is x.m.worker and entry["relative_path"] not in run.started_paths,"NATIVE_EXACT_WORKER")
        run.started_paths.add(entry["relative_path"])
        role=next(i for i,v in enumerate(x.EXPECTED) if v[3]==entry["relative_path"].split("/")[-1])
        intention=c.Child();intention.pidfd=-1;intention.role=role
        child={"entry":entry,"body":None,"pipe":None,"pidfd":None,"pid":None,
          "lifecycle":"LIVE","stop_attempted":False,"wait_status":None,"events":[],"buffer":b"",
          "eof":False,"connected":False,"started":run.clock(),"started_msk":x.m.now(),"recorded":False,"native_intention":intention}
        # Ordinary receipt/FD intent precedes Python body/pipe acquisitions.
        # The original native intention/calls remain the actual native owner;
        # its pidfd is NOT adopted/closed by the ordinary Python slot journal.
        run.children.append(child)
        x.m.sol076_launch_intent(child)
        child["preowned_receiver"]={"pipe_accepted":False,"receipt_accepted":False,"both_accepted":False}
        journal=x.fd_journal(run);config=None
        try:
            body=x.m.sol076_own_fd(child,"body",out.create(entry["relative_path"]))
            read,write=journal.pipe_pair(lambda:os.pipe2(os.O_CLOEXEC))
            x.m.sol076_own_fd(child,"pipe",read)
            x.m.sol076_own_fd(child,"write_pipe",write)
            child["preowned_receiver"].update(fd=read,bound_before_birth=True,
                readonly_parent=True,identity9=journal.current[read]["identity9"],
                exit_is_handover=False,eof_is_handover=False,
                digest_is_handover=False,pending_is_handover=False)
            config=journal.acquire(lambda:os.memfd_create(
                "friday-a061-owned-worker-config",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING))
            raw=json.dumps({"schema":"friday.a061.worker.v1","index":role,"entry":entry,
                 "capsule_sha":self.cap["capsule_sha"]},sort_keys=True,separators=(",",":")).encode()
            x.need(len(raw)<=4096,"WORKER_CONFIG_CAP");x.m.write_all(config,raw)
            fcntl.fcntl(config,fcntl.F_ADD_SEALS,c.SEALS)
            # The native record is allocated before clone and preserves the actual
            # result even if registration throws or Python never receives a PID.
            child["fork_attempted"]=True  # Opaque native call, never guessed no-birth.
            result=self.lib.fr_own_spawn(123,role,config,body,write,130,self.gate[0],int(scheduler_end*10**9),ctypes.byref(intention))
            if intention.pid>0:child.update(pid=intention.pid,pidfd=intention.pidfd,native_birth=intention.birth)
            if result<0:
                if intention.pid>0:self.stop_child(child,run)
                else:
                    child["lifecycle"]="BIRTH_UNCONFIRMED"
                    run.fail("NATIVE_BIRTH_UNCONFIRMED",True)
                run.fail("NATIVE_REGISTER:%d"%result,intention.state==4 or (intention.pid>0 and child["lifecycle"]!="REAPED"))
                raise x.m.Refused("NATIVE_REGISTER:%d"%result)
            x.need(intention.pid>0 and intention.pidfd>=0 and intention.state==2,"NATIVE_REGISTRATION_BARRIER")
            os.set_blocking(read,False)
            return child
        except BaseException as exc:
            x.m.sol076_launch_error(child,exc,"NativeOwner.actual_python_launch")
            run.original_errors.append(exc)
            run.fail(x.cause(exc),child["lifecycle"]=="BIRTH_UNCONFIRMED")
            if child.get("pid") is not None and child["lifecycle"]=="LIVE":
                self.stop_child(child,run)
            x.m.sol076_finish_record(child,{},run,x.r.close_fd)
            raise
        finally:
            x.m.sol076_close_slot(child,"write_pipe",run,x.r.close_fd)
            if x.r.close_fd(config,run) is not True:
                child["sol076"]["cleanup_fault"]=True
                run.fail("NATIVE_CONFIG_CLOSE_UNCONFIRMED")

    def wait(self,child,run):
        result=self.lib.fr_own_wait(123,ctypes.byref(child["native_intention"]),1)
        if result==1:child.update(lifecycle="REAPED",wait_status=child["native_intention"].status);return True
        if result<0:
            child["lifecycle"]="STOP_UNCONFIRMED";run.fail("NATIVE_REAP:%d"%result,True)
            raise self.x.m.Refused("NATIVE_REAP:%d"%result)
        return False

    def stop_child(self,child,run):
        if child["lifecycle"]=="REAPED":return True
        if child["lifecycle"] not in ("LIVE","STOP_UNCONFIRMED"):return False
        if child["stop_attempted"]:return False
        child["stop_attempted"]=True
        end=min(time.monotonic_ns()+10**9,self.cap["hard_ns"]-10**9)
        result=self.lib.fr_own_stop(123,ctypes.byref(child["native_intention"]),end)
        if result==1:child.update(lifecycle="REAPED",wait_status=child["native_intention"].status);return True
        child["lifecycle"]="STOP_UNCONFIRMED";run.fail("NATIVE_STOP:%d"%result,True);return False

    def release_handle(self,child,run):
        if child["pidfd"] is None or child.get("release_attempted"):return
        child["release_attempted"]=True
        result=self.lib.fr_own_release(ctypes.byref(child["native_intention"]))
        child["pidfd"]=child["native_intention"].pidfd if child["native_intention"].pidfd>=0 else None
        if result<0:
            child.update(lifecycle="STOP_UNCONFIRMED",wait_status=None)
            run.fail("NATIVE_CLOSE:%d"%result,True)

    def wave(self,entries,out,context,run,receipt,worker):
        x=self.x;active=[];index=0
        try:
            while active or index<len(entries):
                run.guard("network_schedule");out.check();c.check_loaded_runtime(self.cap)
                for child in list(active):
                    x.r.drain(child,run)
                    done=self.wait(child,run)
                    expired=run.clock()>=min(child["started"]+child["entry"]["seconds"],run.work) or (
                        not child["connected"] and run.clock()>=child["started"]+15)
                    if not done and expired:run.fail("REQUEST_TIMEOUT");self.stop_child(child,run)
                    elif not done:continue
                    x.r.collect(child,out,run,receipt);active.remove(child)
                    self.release_handle(child,run)
                    for key in ("body","pipe"):x.m.sol076_close_slot(child,key,run,x.r.close_fd)
                # All three are registered before the third transport can run.
                # Its benign DATA fixture waits until the two unchanged positive
                # routes were actually reaped, consumed and hashed. This prevents
                # a meaningful third-route negative from preempting positives.
                if self.cap["mode_id"]==4 and index==3 and not self.gate_released:
                    first=[v for v in run.children if v["native_intention"].role in (0,1)]
                    if len(first)==2 and all(v["lifecycle"]=="REAPED" and v["recorded"] for v in first):
                        x.need(not run.reason and not run.uncertain and all(run.results[v["entry"]["relative_path"]]["body_complete"] for v in first),"BENIGN_POSITIVE_PREFIX")
                        x.need(os.write(self.gate[1],b"G")==1,"BENIGN_GATE_WRITE")
                        self.gate_released=True
                if run.reason or run.uncertain:break
                while len(active)<3 and index<len(entries):
                    run.guard("network_schedule")
                    scheduler_end=min([run.work]+[min(v["started"]+v["entry"]["seconds"],v["started"]+15 if not v["connected"] else run.work) for v in active])
                    active.append(self.launch(entries[index],out,context,run,worker,scheduler_end));index+=1
                    run.peak=max(run.peak,len(active))
                if active:
                    c.production_resources(self.cap)
                    select.select([v["pipe"] for v in active],[],[],min(.02,max(0,run.work-run.clock())))
        except BaseException as exc:run.fail(x.cause(exc))
        finally:
            for child in run.children:
                if not child["recorded"]:
                    self.stop_child(child,run)
                    record=x.r.unknown_record(child,"STOP_UNCONFIRMED" if run.uncertain else run.reason or "CONTOUR_ABORTED")
                    run.results[child["entry"]["relative_path"]]=record;run.charged+=child["entry"]["cap"];child["recorded"]=True
                self.release_handle(child,run)
                for key in ("body","pipe"):x.m.sol076_close_slot(child,key,run,x.r.close_fd)
                if child["recorded"]:
                    x.m.sol076_finish_record(child,run.results[child["entry"]["relative_path"]],run,x.r.close_fd)
            for entry in entries:run.results.setdefault(entry["relative_path"],{"state":"NOT_RUN","url":entry["url"],"failure":run.reason,"body_complete":False,"accounting_charged_bytes":0})
            for fd in self.gate:
                if fd>=0:x.r.close_fd(fd,run)
            self.gate=(-1,-1)
            if not run.uncertain and all(v["lifecycle"]=="REAPED" for v in run.children):
                c.command(self.cap,12,13)
