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
        body=out.create(entry["relative_path"]);read,write=os.pipe2(os.O_CLOEXEC)
        intention=c.Child();intention.pidfd=-1;intention.role=role
        child={"entry":entry,"body":body,"pipe":read,"pidfd":None,"pid":None,
          "lifecycle":"LIVE","stop_attempted":False,"wait_status":None,"events":[],"buffer":b"",
          "eof":False,"connected":False,"started":run.clock(),"started_msk":x.m.now(),"recorded":False,"native_intention":intention}
        run.children.append(child)
        config=os.memfd_create("friday-a061-owned-worker-config",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
        try:
            raw=json.dumps({"schema":"friday.a061.worker.v1","index":role,"entry":entry,
                 "capsule_sha":self.cap["capsule_sha"]},sort_keys=True,separators=(",",":")).encode()
            x.need(len(raw)<=4096,"WORKER_CONFIG_CAP");x.m.write_all(config,raw)
            fcntl.fcntl(config,fcntl.F_ADD_SEALS,c.SEALS)
            # The native record is allocated before clone and preserves the actual
            # result even if registration throws or Python never receives a PID.
            result=self.lib.fr_own_spawn(123,role,config,body,write,130,self.gate[0],int(scheduler_end*10**9),ctypes.byref(intention))
            if intention.pid>0:child.update(pid=intention.pid,pidfd=intention.pidfd,native_birth=intention.birth)
            if result<0:
                if intention.pid>0:self.stop_child(child,run)
                else:
                    child["lifecycle"]="NOT_CREATED";run.children.remove(child)
                    x.r.close_fd(read,run);x.r.close_fd(body,run)
                run.fail("NATIVE_REGISTER:%d"%result,intention.state==4 or (intention.pid>0 and child["lifecycle"]!="REAPED"))
                raise x.m.Refused("NATIVE_REGISTER:%d"%result)
            x.need(intention.pid>0 and intention.pidfd>=0 and intention.state==2,"NATIVE_REGISTRATION_BARRIER")
            os.set_blocking(read,False)
            return child
        finally:
            x.r.close_fd(write,run);x.r.close_fd(config,run)

    def wait(self,child,run):
        result=self.lib.fr_own_wait(123,ctypes.byref(child["native_intention"]),1)
        if result==1:child.update(lifecycle="REAPED",wait_status=child["native_intention"].status);return True
        if result<0:
            child["lifecycle"]="STOP_UNCONFIRMED";run.fail("NATIVE_REAP:%d"%result,True)
            raise self.x.m.Refused("NATIVE_REAP:%d"%result)
        return False

    def stop_child(self,child,run):
        if child["lifecycle"]!="LIVE":return child["lifecycle"]=="REAPED"
        if child["stop_attempted"]:return False
        child["stop_attempted"]=True
        end=min(time.monotonic_ns()+10**9,self.cap["hard_ns"]-10**9)
        result=self.lib.fr_own_stop(123,ctypes.byref(child["native_intention"]),end)
        if result==1:child.update(lifecycle="REAPED",wait_status=child["native_intention"].status);return True
        child["lifecycle"]="STOP_UNCONFIRMED";run.fail("NATIVE_STOP:%d"%result,True);return False

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
                    for key in ("body","pipe","pidfd"):x.r.close_fd(child[key],run);child[key]=None
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
                for key in ("body","pipe","pidfd"):x.r.close_fd(child[key],run);child[key]=None
            for entry in entries:run.results.setdefault(entry["relative_path"],{"state":"NOT_RUN","url":entry["url"],"failure":run.reason,"body_complete":False,"accounting_charged_bytes":0})
            for fd in self.gate:
                if fd>=0:x.r.close_fd(fd,run)
            self.gate=(-1,-1)
            if not run.uncertain and all(v["lifecycle"]=="REAPED" for v in run.children):
                c.command(self.cap,12,13)
