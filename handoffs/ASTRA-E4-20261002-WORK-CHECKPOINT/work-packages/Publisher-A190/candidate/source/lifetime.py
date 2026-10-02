"""Preowned return cells and one same-owner descriptor history domain."""
import os
import selectors
import signal
from common import Refused, SLOTS_MAX, mono, error_fact

FD_HISTORY_MAX=65536
FD_ROW_ALLOCATION=8192
FD_HISTORY_ALLOCATION=FD_HISTORY_MAX*FD_ROW_ALLOCATION
_ACTIVE=frozenset(("ACQUIRED","HELD","UNKNOWN"))
_OCCUPIED=_ACTIVE|frozenset(("PREOWNED",))

class FDState:
    """Finite arena charged to the existing owner before any acquisition."""
    def __init__(self,credit_token):
        self.credit_token=credit_token;self.journals=[];self.count=0;self.generation=0
    def attach(self,book):
        if book not in self.journals:self.journals.append(book)
    def unknown(self):
        return any(r["status"]=="UNKNOWN" for b in self.journals for r in b.rows)
    def pending(self):
        return sum(r["status"] in _OCCUPIED for b in self.journals for r in b.rows if r["journal"] is b)
    def completion_ready(self):
        return not any(r['status'] in _OCCUPIED for b in self.journals for r in b.rows if r['journal'] is b)
    def retire_confirmed(self,completion):
        # The native completion receiver first accepted the actual full roots.
        # Closed descriptor facts alone never complete the attached histories.
        if completion is None or not completion.completed or not self.completion_ready():return False
        for book in self.journals:
            if any(getattr(c,'fd_rows',{}) for c in book.grants.values()):return False
        books=list(self.journals)
        for book in books:
            if not book.retire_history(completion):return False
        self.journals.clear();self.count=0;self.generation=0;return True
    def graph(self):
        journals=[b.graph() for b in self.journals]
        return {"arena_token":self.credit_token,"generation":self.generation,"history_count":self.count,
            "history_max":FD_HISTORY_MAX,"journal_count":len(journals),
            "journal_chunks":[journals[i:i+512] for i in range(0,len(journals),512)],"truncated":False}

class OwnedFDs:
    def __init__(self,slot_credit=0,credit=None,meter=None):
        if type(slot_credit) is not int or not 0<=slot_credit<=SLOTS_MAX:raise Refused("fd_acquisition")
        self.slot_credit=slot_credit;self.rows=[];self.grants={};self.faults=[];self.meter=meter;self.state=None
        if credit is not None:self.grant(credit)
    @property
    def fds(self):return {r["fd"] for r in self.rows if r["journal"] is self and r["status"] in _ACTIVE}
    @property
    def meta(self):
        # Last-generation compatibility view; rows retains ALL generations.
        return {r["fd"]:{k:v for k,v in r.items() if k not in ("journal","close_cell")}
            for r in self.rows if r["journal"] is self and r["fd"] is not None}
    def grant(self,credit):
        if credit is None or credit.closed or type(credit.slots) is not int or credit.slots<1:raise Refused("fd_slot_credit")
        owner=getattr(credit,"meter",None) or credit;state=getattr(owner,"fd_state",None)
        if state is None:raise Refused("fd_history_not_preadmitted")
        if self.state is not None and self.state is not state:raise Refused("fd_foreign_owner")
        self.state=state;state.attach(self)
        if self.meter is None and hasattr(owner,"publish_local_owners"):self.meter=owner
        prior=self.grants.get(credit.token)
        if prior is not None and prior is not credit:raise Refused("fd_credit_identity")
        if prior is None:self.grants[credit.token]=credit
        self.slot_credit=sum(c.slots for c in self.grants.values() if not c.closed)
        if self.slot_credit>SLOTS_MAX:raise Refused("fd_acquisition")
        if not hasattr(credit,"fd_rows"):credit.fd_rows={};credit.fd_serial=0
        return credit
    def _slot(self,credit=None,count=1):
        if self.state is not None and self.state.unknown():raise Refused("FD_CLOSE_UNCONFIRMED","terminal")
        if credit is not None:
            if type(credit) in (int,str):credit=self.grants.get(credit)
            if credit is None:raise Refused("fd_slot_credit")
            self.grant(credit);choices=(credit,)
        else:choices=tuple(self.grants.values())
        for c in choices:
            if not c.closed and len(c.fd_rows)+count<=c.slots and self.state.pending()+count<=SLOTS_MAX:return c
        raise Refused("fd_acquisition")
    def _prepare(self,credit,holder,count=1):
        if type(holder) is not str or len(holder)>128:raise Refused("fd_holder")
        c=self._slot(credit,count)
        if self.state.count+count>FD_HISTORY_MAX:raise Refused("fd_history_capacity_before_effect")
        cells=[]
        # Build all cells before publishing any cell or calling a factory.
        # Publication failure still leaves a locally acknowledged NO_RETURN
        # history for every cell actually inserted; no descriptor is guessed.
        for offset in range(1,count+1):
            attempt={"attempt":1,"status":"NOT_ATTEMPTED","error":None}
            row={"fd":None,"holder":holder,"credit":c.token,"status":"PREOWNED","identity9_decimal_strings":None,
                "slot":c.fd_serial+offset,"generation":self.state.generation+offset,"attempted_close":None,"close_history":[],
                "close_cell":attempt,"acquisition_error":None,"last_known_holder":holder,"last_known_credit":c.token,
                "journal":self,"history_arena":self.state.credit_token,"close_policy":None,
                'cancellation_acknowledged':False}
            cells.append(row)
        self.state.generation+=count;c.fd_serial+=count
        try:
            for row in cells:
                self.rows.append(row);self.state.count+=1
                c.fd_rows[row['slot']]=row
            self._changed()
        except BaseException as exc:
            self._cancel(c,cells)
            exc.retained_journal=self
            if self.meter is not None:self.meter.retain_error_arena(exc)
            raise
        return c,cells
    def _cancel(self,c,cells):
        for r in cells:
            r['status']='NO_RETURN'
            r['cancellation_acknowledged']=True
            if c.fd_rows.get(r['slot']) is r:c.fd_rows.pop(r['slot'])
        # Histories are retained in the same arena until full completion.
        # Cancellation is finite, allocation-free and never calls the factory.
    def _bind(self,r,fd):
        # Existing preowned fields only: no insert/allocation callback/fstat.
        r["fd"]=fd;r["status"]="ACQUIRED"
    def _row(self,fd):
        return next((r for r in reversed(self.rows) if r["fd"]==fd and r["journal"] is self and r["status"] in _ACTIVE),None)
    def _changed(self):
        if self.meter is not None:self.meter.publish_local_owners()
    def _identity9(self,fd):
        s=os.fstat(fd)
        return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
    def _project(self,r):
        try:
            r["identity9_decimal_strings"]=self._identity9(r["fd"])
            r["close_policy"]="KEEP" if os.get_inheritable(r["fd"]) else "EXEC_CLOEXEC"
        except BaseException as exc:
            r["acquisition_error"]=error_fact(exc,"terminal");exc.retained_journal=self
            try:self._close_row(r)
            except BaseException as secondary:self.faults.append(error_fact(secondary,"terminal"))
            raise
        r["status"]="HELD";self._changed();return r["fd"]
    def acquire(self,factory,*args,holder="root",credit=None,**kwargs):
        c,cells=self._prepare(credit,holder)
        try:fd=factory(*args,**kwargs)
        except BaseException:self._cancel(c,cells);raise
        self._bind(cells[0],fd);return self._project(cells[0])
    def pair(self,flags,credit=None):
        c,cells=self._prepare(credit,"pipe",2)
        try:left,right=os.pipe2(flags)
        except BaseException:self._cancel(c,cells);raise
        self._bind(cells[0],left);self._bind(cells[1],right)
        try:self._project(cells[0]);self._project(cells[1])
        except BaseException as exc:exc.retained_journal=self;raise
        return left,right
    def own(self,fd,holder="root",credit=None,identity=None):
        if self._row(fd) is None:raise Refused("fd_requires_prospective_acquisition")
        return fd
    def release_returned(self,fd):raise Refused("fd_requires_same_owner_transfer")
    def transfer(self,fd,dest):
        if dest is self:return fd
        r=self._row(fd)
        if r is None:raise Refused("fd_transfer")
        dest.grant(self.grants[r["credit"]])
        if dest.state is not self.state or dest._row(fd) is not None:raise Refused("fd_transfer")
        # Original retains the row during the only fallible destination insert.
        dest.rows.append(r);r["journal"]=dest;dest._changed();return fd
    def _close_row(self,r):
        if r["status"] not in _ACTIVE or r["journal"] is not self or r["status"]=="UNKNOWN":return
        attempt=r["close_cell"];r["close_history"].append(attempt)
        r["attempted_close"]="ATTEMPTED";attempt["status"]="ATTEMPTED"
        try:os.close(r["fd"])
        except BaseException as exc:
            # Numeric reuse and a later EBADF probe never prove this close.
            r["status"]="UNKNOWN";r["attempted_close"]="UNKNOWN";attempt["status"]="UNKNOWN"
            attempt["error"]=error_fact(exc,"terminal");self.faults.append(attempt["error"])
        else:
            r["status"]="CLOSED";r["attempted_close"]="CLOSED";attempt["status"]="CLOSED"
            self.grants[r["credit"]].fd_rows.pop(r["slot"],None)
        self._changed()
    def close_one(self,fd):
        r=self._row(fd)
        if r is not None:self._close_row(r)
    def close(self,exclude=()):
        for r in self.rows:
            if r["fd"] not in exclude:self._close_row(r)
        return list(self.faults)
    def graph(self):
        rows=[{k:v for k,v in r.items() if k not in ("journal","close_cell")} for r in self.rows if r["journal"] is self]
        return {"journal_chunks":[rows[i:i+512] for i in range(0,len(rows),512)],"journal_count":len(rows),
            "pending_fds":sorted(self.fds),"faults":list(self.faults),"history_arena":self.state.credit_token if self.state else None,
            "truncated":False,"credits":[{"token":c.token,"slots":c.slots,"closed":c.closed} for c in self.grants.values()]}
    def retire_history(self,completion=None):
        if completion is None or not completion.completed:return False
        if any(r['status'] in _OCCUPIED for r in self.rows if r['journal'] is self):return False
        if any(getattr(c,'fd_rows',{}) or not c.closed for c in self.grants.values()):return False
        self.rows.clear();self.grants.clear();self.faults.clear();self.meter=None;self.state=None
        return True

class ForkOwner:
    """Prospective child table cells prepared in the same existing Root.

    Parent and child tables have distinct close histories after fork. The
    parent keeps this prepared object and a prepaid dedicated receipt pipe;
    the child never uses stderr as its ownership transport.
    """
    def __init__(self,observer,stock_credit):
        self.observer=observer;self.parent_pid=os.getpid();self.pid=None
        self.credit=stock_credit;self.rows=[];self.parent_rows=[];self.faults=[]
        self.state=None;self.grants={stock_credit.token:stock_credit}
        self.export_attempted=False
        self.final_failure_export_attempted=False
        source={}
        for book in observer.fd_state.journals:
            for row in book.rows:
                if row["journal"] is book and row["status"] in _ACTIVE:source[row["fd"]]=(book,row)
        # listdir's ephemeral directory FD is not an inherited acquisition.
        # Any named Source UNKNOWN row refuses fork before this census.
        if observer.fd_state.unknown():raise Refused("FD_CLOSE_UNCONFIRMED","terminal")
        census=[int(x) for x in os.listdir('/proc/self/fd')]
        if len(census)>SLOTS_MAX+1:raise Refused("inherited_FD_capacity_before_fork")
        for fd in census:
            prior=source.get(fd)
            row={"fd":fd,"holder":"child-inherited","credit":prior[1]["credit"] if prior else stock_credit.token,
                "slot":prior[1]["slot"] if prior else None,"status":"PREOWNED_CHILD_TABLE",
                "generation":None,"parent_pid":self.parent_pid,"identity9_decimal_strings":None,
                "parent_acquisition":None,"attempted_close":None,"close_history":[],
                "close_cell":{"attempt":1,"status":"NOT_ATTEMPTED","error":None},
                "close_policy":None,"journal":self,"last_known_holder":"child-inherited",
                "last_known_credit":prior[1]["credit"] if prior else stock_credit.token}
            self.rows.append(row)
            try:
                s=os.fstat(fd)
                row["identity9_decimal_strings"]=[str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
                row["close_policy"]="KEEP" if os.get_inheritable(fd) else "EXEC_CLOEXEC"
            except OSError as exc:
                if prior is None and exc.errno==9:row["status"]="NO_CHILD_ACQUISITION_CENSUS_EBADF";continue
                row["status"]="UNKNOWN";raise
            if prior:
                book,original=prior;self.grants[original["credit"]]=book.grants[original["credit"]]
                row["parent_acquisition"]={k:v for k,v in original.items() if k not in ("journal","close_cell")}
                self.parent_rows.append((row,original))
            else:
                if not hasattr(stock_credit,'fd_rows'):stock_credit.fd_rows={};stock_credit.fd_serial=0
                stock_credit.fd_serial+=1;row["slot"]=stock_credit.fd_serial
                stock_credit.fd_rows[row["slot"]]=row
        self.parent_state=observer.fd_state
    @property
    def fds(self):return {r['fd'] for r in self.rows if r['status'] in _ACTIVE}
    def adopt(self):
        if os.getppid()!=self.parent_pid:raise Refused("child_table_parent")
        self.pid=os.getpid();self.state=FDState(self.credit.token)
        self.observer.fd_state=self.state;self.state.attach(self)
        for child,original in self.parent_rows:
            original['status']='PARENT_SNAPSHOT_TRANSFERRED_TO_CHILD_TABLE'
            self.grants[child['credit']].fd_rows[child['slot']]=child
        for r in self.rows:
            if r['status']=='PREOWNED_CHILD_TABLE':
                self.state.generation+=1;r['generation']=self.state.generation;r['status']='HELD'
                self.state.count+=1
    def close_one(self,fd):
        row=next((r for r in reversed(self.rows) if r['fd']==fd and r['status'] in _ACTIVE),None)
        if row is None or row['status']=='UNKNOWN':return
        attempt=row['close_cell'];row['close_history'].append(attempt)
        row['attempted_close']='ATTEMPTED';attempt['status']='ATTEMPTED'
        try:os.close(fd)
        except BaseException as exc:
            row['status']='UNKNOWN';row['attempted_close']='UNKNOWN';attempt['status']='UNKNOWN'
            attempt['error']=error_fact(exc,'terminal');self.faults.append(attempt['error'])
        else:
            row['status']='CLOSED';row['attempted_close']='CLOSED';attempt['status']='CLOSED'
            self.grants[row['credit']].fd_rows.pop(row['slot'],None)
    def keep(self,fd):
        os.set_inheritable(fd,True)
        for book in self.state.journals:
            for r in book.rows:
                if r['fd']==fd and r['status'] in _ACTIVE:r['close_policy']='KEEP'
    def graph(self):
        rows=[{k:v for k,v in r.items() if k not in ('journal','close_cell')} for r in self.rows]
        return {'owner_pid':self.pid,'parent_pid':self.parent_pid,'journal_chunks':[rows[i:i+512] for i in range(0,len(rows),512)],
            'journal_count':len(rows),'pending_fds':sorted(self.fds),'faults':list(self.faults),'truncated':False,
            'credits':[{'token':c.token,'slots':c.slots,'closed':c.closed} for c in self.grants.values()]}
    def emit(self,fd):
        if self.export_attempted:raise Refused('child_owner_export_already_attempted','terminal')
        self.export_attempted=True
        from common import canonical,INPUT_MAX
        import struct
        graph={'schema':'friday.a181.fork-owner-graph.v1','owner_pid':self.pid,'parent_pid':self.parent_pid,
            'inherited':self.graph(),'new_journals':self.state.graph(),'delivery_fd':fd,'truncated':False}
        raw=canonical(graph,maximum=INPUT_MAX)
        for blob in (struct.pack('>Q',len(raw)),raw):
            at=0
            while at<len(blob):
                n=os.write(fd,memoryview(blob)[at:])
                if n<=0:raise Refused('child_owner_delivery','terminal')
                at+=n
    def retire_history(self,completion=None):
        if completion is None or not completion.completed:return False
        if self.fds or any(getattr(c,'fd_rows',{}) or not c.closed for c in self.grants.values()):return False
        self.rows.clear();self.parent_rows.clear();self.faults.clear();self.grants.clear()
        self.observer=self.parent_state=self.state=self.credit=None
        return True

    def emit_failure(self,fd,error):
        # Pre-exec receipt does not represent later exec/configuration failure.
        if self.final_failure_export_attempted:raise Refused('child_final_failure_attempted','terminal')
        self.final_failure_export_attempted=True
        from observer import full_value_arena
        from common import canonical,INPUT_MAX
        import struct
        values=full_value_arena([error,self.graph(),self.state.graph() if self.state is not None else None])
        graph={'schema':'friday.a190.fork-final-failure.v1','owner_pid':os.getpid(),
            'parent_pid':self.parent_pid,'phase':'FINAL_FAILURE','values':values,
            'error':error_fact(error,'terminal'),'complete':values['complete'],'truncated':False}
        raw=canonical(graph,maximum=INPUT_MAX)
        for blob in (struct.pack('>Q',len(raw)),raw):
            at=0
            while at<len(blob):
                count=os.write(fd,memoryview(blob)[at:])
                if count<=0:raise Refused('child_final_failure_delivery','terminal')
                at+=count

def receive_fork_owner(fd,hold,deadline,pid,parent_pid,final_failure=False,allow_eof=False):
    """Only prepaid fixed framing/parse; not ordinary deadline admission."""
    from common import parse,INPUT_MAX,json_preflight
    import struct
    def receive(n):
        data=bytearray(n);at=0
        while at<n:
            left=(deadline-mono())/1e9
            if left<=0:raise Refused('child_owner_deadline','terminal')
            with selectors.PollSelector() as selected:
                selected.register(fd,selectors.EVENT_READ)
                if not selected.select(left):raise Refused('child_owner_deadline','terminal')
            part=os.read(fd,min(65536,n-at))
            if not part:
                if allow_eof and at==0 and n==8:return None
                raise Refused('child_owner_eof','terminal')
            data[at:at+len(part)]=part;at+=len(part);hold.commit(reads=len(part))
        return bytes(data)
    header=receive(8)
    if header is None:return None
    n=struct.unpack('>Q',header)[0]
    if n>INPUT_MAX:raise Refused('child_owner_capacity','terminal')
    raw=receive(n);value=parse(raw,maximum=INPUT_MAX)
    failure=value.get('schema')=='friday.a190.fork-final-failure.v1'
    schema='friday.a190.fork-final-failure.v1' if final_failure or failure else 'friday.a181.fork-owner-graph.v1'
    if value.get('schema')!=schema or value.get('owner_pid')!=pid or value.get('parent_pid')!=parent_pid or value.get('truncated') is not False:
        raise Refused('child_owner_identity','terminal')
    if final_failure or failure:
        if value.get('phase')!='FINAL_FAILURE' or type(value.get('values')) is not dict:
            raise Refused('child_final_failure_schema','terminal')
    hold.commit(output=n+8)
    value['_receiver_resident_upper']=json_preflight(raw)+len(raw)*4+131072
    return value

def stock_inherited_upper(observer):
    source={fd for book in observer.fd_state.journals for fd in book.fds}
    return len({int(x) for x in os.listdir('/proc/self/fd')}-source)

def bounded_direct_reap(observer,pid,pidfd,kill=False):
    """No blocking wait4(0); every unconfirmed owner remains retained."""
    faults=[]
    if kill:
        try:signal.pidfd_send_signal(pidfd,signal.SIGKILL,None,0)
        except ProcessLookupError:pass
        except BaseException as exc:faults.append(error_fact(exc,"terminal"))
    deadline=min(observer.deadline,mono()+10_000_000_000)
    with selectors.PollSelector() as selected:
        selected.register(pidfd,selectors.EVENT_READ)
        while mono()<deadline:
            if selected.select(min(0.1,(deadline-mono())/1e9)):
                try:observer.before_reap(pid)
                except BaseException as exc:faults.append(error_fact(exc,"terminal"))
                waited,status,usage=os.wait4(pid,os.WNOHANG)
                if waited!=pid:raise Refused("direct_wait4_identity","terminal")
                observer.record_wait4(waited,status,usage);return status,usage,faults
    observer.note_cleanup({"cause":"STOP_UNCONFIRMED","pid":pid,"pidfd_retained":True,"slot_retained":True})
    raise Refused("STOP_UNCONFIRMED","terminal",{"pid":pid,"faults":faults})
