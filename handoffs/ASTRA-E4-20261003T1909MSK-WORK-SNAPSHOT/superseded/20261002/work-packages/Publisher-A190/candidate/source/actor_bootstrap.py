"""Future independently Root-owned held-exec bootstrap; stock imports only."""
import hashlib
import importlib.abc
import importlib.util
import json
import os
import sys
import struct

class StockChildJournal:
    """Stock bridge to the exact table prospectively owned before fork/exec."""
    def __init__(self,fds):
        self.rows=[];self.grants={};self.faults=[];self.state=None;self.meter=None
        for fd in fds:
            self.rows.append({"fd":fd,"status":"ROOT_PREOWNED_BIND_PENDING","credit":None,
                "identity9_decimal_strings":None,"generation":None,"slot":None,
                "holder":"bootstrap-inherited","last_known_holder":"bootstrap-inherited",
                "last_known_credit":None,"attempted_close":None,"close_history":[],
                "close_cell":{"attempt":1,"status":"NOT_ATTEMPTED","error":None},"journal":self})
    @property
    def fds(self):return {r['fd'] for r in self.rows if r['status'] in ('HELD','UNKNOWN','ROOT_PREOWNED_BIND_PENDING')}
    def bind(self,packet,pid):
        if packet['owner_pid']!=pid or packet['parent_pid']!=os.getppid():raise RuntimeError('bootstrap-owner-identity')
        inherited=[r for chunk in packet['inherited']['journal_chunks'] for r in chunk]
        latest={r['fd']:r for r in inherited if r['status']=='HELD'}
        for bookchunk in packet['new_journals']['journal_chunks']:
            for book in bookchunk:
                for chunk in book.get('journal_chunks',[]):
                    for r in chunk:
                        if r['status']=='HELD':latest[r['fd']]=r
        for row in self.rows:
            prior=latest.get(row['fd'])
            if prior is None:raise RuntimeError('bootstrap-owner-row')
            info=os.fstat(row['fd'])
            identity=[str(x) for x in (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)]
            if identity!=prior['identity9_decimal_strings']:raise RuntimeError('bootstrap-owner-drift')
            row.update({k:v for k,v in prior.items() if k not in ('journal','close_cell')})
            row['parent_acquisition']=prior;row['journal']=self
        self.credit_rows=packet['inherited']['credits']
        credits={r['token']:r for r in self.credit_rows}
        for chunk in packet['new_journals']['journal_chunks']:
            for book in chunk:
                for credit in book.get('credits',[]):credits[credit['token']]=credit
        self.credit_rows=list(credits.values())
        known={row['fd'] for row in self.rows}
        for fd,prior in latest.items():
            if fd not in known and prior.get('close_policy')=='KEEP':
                row=dict(prior);row['parent_acquisition']=prior;row['journal']=self
                row['close_cell']={'attempt':1,'status':'NOT_ATTEMPTED','error':None}
                self.rows.append(row)
        self.pre_exec_owner_graph=packet
    def connect(self,meter):
        from observer import Reservation
        self.meter=meter;self.state=meter.fd_state;self.state.attach(self)
        for fact in self.credit_rows:
            c=Reservation(meter,fact['token'],fact['slots']);c.fd_rows={};c.fd_serial=0
            self.grants[c.token]=c
        for row in self.rows:
            c=self.grants[row['credit']];c.fd_rows[row['slot']]=row;c.fd_serial=max(c.fd_serial,row['slot'])
            self.state.count+=1;self.state.generation=max(self.state.generation,row['generation'])
        meter.retain_local_owner(self)
    def close_one(self,fd):
        r=next((r for r in self.rows if r['fd']==fd and r['status']=='HELD'),None)
        if r is None:return
        a=r['close_cell'];r['close_history'].append(a);a['status']='ATTEMPTED';r['attempted_close']='ATTEMPTED'
        try:os.close(fd)
        except BaseException as exc:
            r['status']='UNKNOWN';r['attempted_close']='UNKNOWN';a['status']='UNKNOWN'
            a['error']={'class':type(exc).__name__,'errno':getattr(exc,'errno',None),'text':str(exc)};self.faults.append(a['error'])
        else:
            r['status']='CLOSED';r['attempted_close']='CLOSED';a['status']='CLOSED'
            if self.grants:self.grants[r['credit']].fd_rows.pop(r['slot'],None)
        if self.meter is not None:self.meter.publish_local_owners()
    def graph(self):
        rows=[{k:v for k,v in r.items() if k not in ('journal','close_cell')} for r in self.rows]
        return {'journal_chunks':[rows], 'journal_count':len(rows),'pending_fds':sorted(self.fds),
            'faults':self.faults,'truncated':False,'credits':self.credit_rows,
            'pre_exec_owner_graph':self.pre_exec_owner_graph}


class StockChannel:
    """Reviewed-stock bootstrap framing on the already Root-created pipes.

    No selected Source import is needed here. The parent owns the complete
    bundle/parser/initial transport allocation before fork, and supplies the
    exact live bundle hash token only in its sealed authenticated bundle.
    """
    def __init__(self,outgoing,incoming,owner_export_token=None):
        self.outgoing,self.incoming=outgoing,incoming
        self.owner_export_token=owner_export_token;self.last_raw=None
        self.last_prepaid=False;self.prefix_attempted=False
    def read(self):
        def receive(size):
            result=bytearray()
            while len(result)<size:
                part=os.read(self.incoming,min(65536,size-len(result)))
                if not part:raise RuntimeError("bootstrap-control-eof")
                result.extend(part)
            return bytes(result)
        header=struct.unpack('>Q',receive(8))[0]
        self.last_prepaid=bool(header&(1<<63));size=header&((1<<63)-1)
        if self.last_prepaid and self.owner_export_token is None:raise RuntimeError('bootstrap-prepaid-not-owned')
        if size>2_000_000:raise RuntimeError("bootstrap-control-size")
        def pairs(rows):
            result={}
            for key,value in rows:
                if key in result:raise RuntimeError("bootstrap-duplicate-key")
                result[key]=value
            return result
        self.last_raw=receive(size)
        return json.loads(self.last_raw,object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(RuntimeError("bootstrap-numeric")))
    def commit_hash(self,token,size):
        # Only this bounded fixed scalar request exists before Source admit.
        if type(token) is not int or type(size) is not int or size<0:
            raise RuntimeError("bootstrap-credit")
        raw=json.dumps({"type":"commit","token":token,"reads":0,"output":0,"hash_bytes":size},
            sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("ascii")+b"\n"
        for body in (struct.pack(">Q",len(raw)),raw):
            at=0
            while at<len(body):
                written=os.write(self.outgoing,memoryview(body)[at:])
                if written<=0:raise RuntimeError("bootstrap-control-short")
                at+=written
        response=self.read()
        if response.get('ok') is not True:
            error=RuntimeError('bootstrap-credit-refused')
            error.root_response=response;error.root_response_raw=self.last_raw
            raise error
    def fail_prefix(self,book,error,raw,value,launch):
        if self.prefix_attempted or self.owner_export_token is None:raise RuntimeError('bootstrap-prefix-attempt')
        self.prefix_attempted=True
        # Stock serialization is available before any selected Source import.
        # The Root already retains the actual sealed bundle/pre-exec packet.
        # Actual traceback/local objects that stock JSON cannot carry remain
        # explicit: this prefix is retained, never acknowledged as complete.
        tb=error.__traceback__;frames=[]
        while tb is not None:
            frames.append({'filename':tb.tb_frame.f_code.co_filename,'name':tb.tb_frame.f_code.co_name,
                'line':tb.tb_lineno,'locals_identity':str(id(tb.tb_frame.f_locals))})
            tb=tb.tb_next
        rows=[{k:v for k,v in row.items() if k not in ('journal','close_cell')} for row in book.rows]
        packet={'type':'bootstrap-owner-prefix','owner_pid':os.getpid(),'prepaid_token':self.owner_export_token,
            'schema':'friday.a190.stock-bootstrap-prefix.v1','rows':rows,
            'raw_bundle':None if raw is None else raw.hex(),'decoded_bundle_present':value is not None,
            'pre_exec_launch':launch,'root_response_raw':None if self.last_raw is None else self.last_raw.hex(),
            'error':{'module':type(error).__module__,'class':type(error).__name__,'text':str(error)},
            'traceback_frames':frames,'complete':False,'truncated':False}
        wire=json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')+b'\n'
        if len(wire)>2_000_000:raise RuntimeError('bootstrap-complete-prefix-wire-bound-open')
        for part in (struct.pack('>Q',len(wire)|(1<<63)),wire):
            at=0
            while at<len(part):
                count=os.write(self.outgoing,memoryview(part)[at:])
                if count<=0:raise RuntimeError('bootstrap-prefix-delivery')
                at+=count
        reply=self.read()
        if reply.get('ok') is not True:raise RuntimeError('bootstrap-prefix-receiver-refused')
        return reply['value']


class VerifiedSource(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def __init__(self, rows):
        self.rows={}
        if type(rows) is not list or not 1<=len(rows)<=128:raise RuntimeError("source-bundle-count")
        for row in rows:
            if set(row)!={"name","path","sha256","source"} or row["name"] in self.rows:
                raise RuntimeError("source-bundle-schema")
            raw=row["source"].encode("utf-8")
            if len(raw)>2_000_000 or type(row["sha256"]) is not str or len(row["sha256"])!=64:
                raise RuntimeError("source-bundle-sha")
            self.rows[row["name"]]=(row["path"],raw,row["sha256"])
        self._pending=True
    def admit(self, channel,token):
        if channel is None or not self._pending:
            raise RuntimeError("source-bundle-sha")
        for name,(path,raw,expected) in self.rows.items():
            channel.commit_hash(token,len(raw))
            if hashlib.sha256(raw).hexdigest()!=expected:
                raise RuntimeError("source-bundle-sha")
            self.rows[name]=(path,raw)
        self._pending=False
    def find_spec(self,name,path=None,target=None):
        if name in self.rows:return importlib.util.spec_from_loader(name,self,origin=self.rows[name][0])
        return None
    def create_module(self,spec):return None
    def exec_module(self,module):
        if self._pending:raise RuntimeError("source-bundle-sha")
        path,raw=self.rows[module.__name__]
        module.__file__=path
        exec(compile(raw,path,"exec",dont_inherit=True),module.__dict__)


def main():
    if len(sys.argv)!=5:raise RuntimeError("bootstrap-argv")
    bundle_fd,request_fd,response_fd,export_token=map(int,sys.argv[1:])
    child_book=StockChildJournal((bundle_fd,request_fd,response_fd))
    stock=StockChannel(request_fd,response_fd,export_token)
    raw=value=launch=loader=meter=consumer=channel=None
    try:
        return _perform_bootstrap(bundle_fd,request_fd,response_fd,export_token,child_book,stock)
    except BaseException as primary:
        # The broad stock boundary includes bind/admit/import/connect/configure
        # and constructor failure, not just the ordinary consumer body.
        frame=primary.__traceback__
        while frame is not None:
            if frame.tb_frame.f_code.co_name=='_perform_bootstrap':
                held=frame.tb_frame.f_locals
                raw=held.get('raw');value=held.get('value');launch=held.get('launch')
                meter=held.get('meter')
                break
            frame=frame.tb_next
        if meter is None or not meter.owner_export_attempted:
            try:stock.fail_prefix(child_book,primary,raw,value,launch)
            except BaseException as export_error:primary.owner_export_error=export_error
        raise

def _perform_bootstrap(bundle_fd,request_fd,response_fd,export_token,child_book,stock):
    import fcntl
    seals=fcntl.fcntl(bundle_fd,fcntl.F_GET_SEALS)
    required=fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
    if seals&required!=required:raise RuntimeError("bundle-not-sealed")
    size=os.fstat(bundle_fd).st_size
    if not 1<=size<=2_000_000:raise RuntimeError("bundle-size")
    raw=os.pread(bundle_fd,size,0)
    if len(raw)!=size:raise RuntimeError("bundle-short")
    # The parent authenticated admission and verified all source bytes before
    # creating this sealed memfd. A caller fd/label is never Root provenance.
    value=json.loads(raw)
    if set(value)!={"sources","admission","ordinary","parent_pid","consumer_files","bootstrap_hash_token","owner_export_token"}:
        raise RuntimeError("bundle-fields")
    if value["parent_pid"]!=os.getppid():raise RuntimeError("bootstrap-parent")
    loader=VerifiedSource(value["sources"])
    launch=stock.read()["launch_fact"]
    child_book.bind(launch['bootstrap_child_ownership'],os.getpid())
    loader.admit(stock,value["bootstrap_hash_token"])
    sys.meta_path.insert(0,loader)
    from launcher import actor_main, ControlChannel
    from observer import MeterRPC
    from consumer_bridge import HeldConsumerLoader
    # One Root channel owns preparation/import admission as well as execution.
    if value['owner_export_token']!=export_token:raise RuntimeError('bootstrap-export-token-drift')
    channel=ControlChannel(request_fd,response_fd,child_book,export_token)
    meter=MeterRPC(channel.call,value['owner_export_token'],channel.terminal_call)
    consumer=None
    try:
        child_book.connect(meter)
        channel.configure(meter)
        consumer=HeldConsumerLoader(value["consumer_files"],meter)
        consumer.acquire().enter()
        actor_main(request_fd,response_fd,value["admission"],launch,value["ordinary"],consumer.module,
                   initial_launch=launch,existing_channel=channel,existing_meter=meter)
    finally:
        primary=sys.exc_info()[1]
        if primary is not None:meter.retain_error_arena(primary)
        cleanup_error=None
        try:
            if consumer is not None:consumer.close()
        except BaseException as exc:
            cleanup_error=exc;meter.retain_error_arena(exc)
        child_book.close_one(bundle_fd)
        # This is the sole final attempt, after all ordinary/bootstrap close
        # facts. The final two transport rows deliberately remain HELD in the
        # complete accepted graph until Root confirms process/table retirement.
        # No later local close can create unexported UNKNOWN histories.
        try:meter.publish_local_owners(terminal=True)
        except BaseException as exc:
            meter.retain_error_arena(exc)
            if primary is None and cleanup_error is None:raise
            if primary is not None:primary.owner_export_error=exc
            else:cleanup_error.owner_export_error=exc
        if primary is None and cleanup_error is not None:raise cleanup_error


if __name__=="__main__":
    main()
