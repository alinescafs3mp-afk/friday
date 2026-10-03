"""Preowned return cells and one same-owner descriptor history domain."""
import os
import selectors
import signal
from common import (Refused, SLOTS_MAX, mono, error_fact, encode_history_rows,
    history_collection, _history_chunks, fork_history_book)

FD_HISTORY_MAX=65536
FD_ROW_ALLOCATION=8192
FD_HISTORY_ALLOCATION=FD_HISTORY_MAX*FD_ROW_ALLOCATION
_ACTIVE=frozenset(("ACQUIRED","HELD","UNKNOWN"))
_OCCUPIED=_ACTIVE|frozenset(("PREOWNED","PREOWNED_CHILD_TABLE"))
_LIVE_FD_STATES=[]

def live_fd_states():
    return tuple(_LIVE_FD_STATES)

def collection_for(rows, state, identity):
    return history_collection(len(rows),identity,None if state is None else state.generation,
        None if state is None else str(id(state)),os.getpid())

class FDState:
    """Finite arena charged to the existing owner before any acquisition."""
    def __init__(self,credit_token):
        self.credit_token=credit_token;self.journals=[];self.count=0;self.generation=0
        self.row_ids=set();_LIVE_FD_STATES.append(self)
    def note_row(self,row):
        self.row_ids.add(id(row))
    def forget_row(self,row):
        self.row_ids.discard(id(row))
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
        self.journals.clear();self.count=0;self.generation=0;self.row_ids.clear()
        if self in _LIVE_FD_STATES:_LIVE_FD_STATES.remove(self)
        return True
    def graph(self):
        journals=[b.graph() for b in self.journals]
        return {"domain_identity":str(id(self)),"arena_token":self.credit_token,"generation":self.generation,"history_count":self.count,
            "history_max":FD_HISTORY_MAX,"journal_count":len(journals),
            "journal_chunks":_history_chunks(journals),"truncated":False}

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
                self.rows.append(row);self.state.note_row(row);self.state.count+=1
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
        dest.rows.append(r);r["journal"]=dest
        if dest.state is not None:dest.state.note_row(r)
        dest._changed();return fd
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
        credits=[{"token":c.token,"slots":c.slots,"closed":c.closed} for c in self.grants.values()]
        return {"history_codec":encode_history_rows(rows),"journal_count":len(rows),
            "pending_fds":sorted(self.fds),"fault_count":len(self.faults),"fault_chunks":_history_chunks(self.faults),
            "history_arena":self.state.credit_token if self.state else None,
            "truncated":False,"credit_count":len(credits),"credit_chunks":_history_chunks(credits),
            "collection":collection_for(rows,self.state,id(self.rows))}
    def retire_history(self,completion=None):
        if completion is None or not completion.completed:return False
        if any(r['status'] in _OCCUPIED for r in self.rows if r['journal'] is self):return False
        if any(getattr(c,'fd_rows',{}) or not c.closed for c in self.grants.values()):return False
        if self.state is not None:
            for row in self.rows:self.state.forget_row(row)
        self.rows.clear();self.grants.clear();self.faults.clear();self.meter=None;self.state=None
        return True

class ForkOwner:
    """Prospective child table cells prepared in the same existing Root.

    Parent and child tables have distinct close histories after fork. The
    parent keeps this prepared object and a prepaid dedicated receipt pipe;
    the child never uses stderr as its ownership transport.
    """
    def __init__(self,observer,stock_credit,body_binding=None,ack_fd=None,deadline=None,failure_mailbox=None):
        self.observer=observer;self.parent_pid=os.getpid();self.pid=None
        self.credit=stock_credit;self.rows=[];self.parent_rows=[];self.faults=[]
        self.state=FDState(stock_credit.token);self.state.attach(self)
        self.grants={stock_credit.token:stock_credit}
        self.failure_errors=[]
        self.parent_table_end=None
        self.parent_child_pid=None
        self.export_attempted=False
        self.final_failure_export_attempted=False
        self.body_binding=body_binding;self.body_ack_fd=ack_fd;self.body_deadline=deadline
        self.body_count=0;self.body_transport=0;self.body_aliases=[];self.body_fd=-1
        self.body_finished=False;self.body_gate_seen=False;self.body_owner_ack_seen=False
        source={}
        for book in observer.fd_state.journals:
            for row in book.rows:
                if row["journal"] is book and row["status"] in _ACTIVE:source[row["fd"]]=(book,row)
        # listdir's ephemeral directory FD is not an inherited acquisition.
        # Any named Source UNKNOWN row refuses fork before this census.
        if observer.fd_state.unknown():raise Refused("FD_CLOSE_UNCONFIRMED","terminal")
        census=[int(x) for x in os.listdir('/proc/self/fd')]
        if len(census)>SLOTS_MAX+1:raise Refused("inherited_FD_capacity_before_fork")
        self.failure_mailbox=failure_mailbox
        for fd in census:
            prior=source.get(fd)
            row={"fd":fd,"holder":"child-inherited","credit":prior[1]["credit"] if prior else stock_credit.token,
                "slot":prior[1]["slot"] if prior else None,"status":"PREOWNED_CHILD_TABLE",
                "generation":None,"parent_pid":self.parent_pid,"identity9_decimal_strings":None,
                "parent_acquisition":None,"attempted_close":None,"close_history":[],
                "close_cell":{"attempt":1,"status":"NOT_ATTEMPTED","error":None},
                "close_policy":None,"journal":self,"last_known_holder":"child-inherited",
                "last_known_credit":prior[1]["credit"] if prior else stock_credit.token,
                "cancellation_acknowledged":False}
            self.state.generation+=1;row['generation']=self.state.generation
            self.rows.append(row)
            self.state.note_row(row);self.state.count+=1
            try:
                s=os.fstat(fd)
                row["identity9_decimal_strings"]=[str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
                row["close_policy"]="KEEP" if os.get_inheritable(fd) else "EXEC_CLOEXEC"
            except OSError as exc:
                if prior is None and exc.errno==9:
                    row["status"]="NO_CHILD_ACQUISITION_CENSUS_EBADF"
                    row["cancellation_acknowledged"]=True
                    continue
                row["status"]="NO_RETURN"
                row["cancellation_acknowledged"]=True
                row["acquisition_error"]=error_fact(exc,'terminal')
                self._acknowledge_unadopted_census(stock_credit)
                raise
            if prior:
                book,original=prior;self.grants[original["credit"]]=book.grants[original["credit"]]
                row["parent_acquisition"]={k:v for k,v in original.items() if k not in ("journal","close_cell")}
                self.parent_rows.append((row,original))
            else:
                try:
                    if not hasattr(stock_credit,'fd_rows'):stock_credit.fd_rows={};stock_credit.fd_serial=0
                    stock_credit.fd_serial+=1;row["slot"]=stock_credit.fd_serial
                    stock_credit.fd_rows[row["slot"]]=row
                except BaseException:
                    self._acknowledge_unadopted_census(stock_credit)
                    raise
        self.parent_state=observer.fd_state
        # SAME already charged child-table domain fully prepared in Root before
        # fork. No child allocation/publication must create its history identity,
        # chronology or row-id membership after an ordinary adoption error.
        # Parent observer.fd_state and its PID guard are never substituted here.
        # Constructor/publication failure stays in the retained partial parent
        # object; it never certifies a child table or native finite end.
        # Generation/membership/count are committed per actual inserted row
        # above, before fstat/constructor failures; no second chronology/reset.
    @property
    def fds(self):
        return {r['fd'] for r in self.rows
            if r['status'] in _ACTIVE or r['status']=='PREOWNED_CHILD_TABLE'}
    def _acknowledge_unadopted_census(self, stock_credit):
        """Finite cancel of PREOWNED_CHILD_TABLE rows inserted before a later fstat failure.

        OwnedFDs publication failure already acknowledges its own cells. This path
        does not clear an UNKNOWN fstat and does not retire the child journal.
        """
        rows=getattr(stock_credit,"fd_rows",None)
        if type(rows) is dict:
            for slot, held in list(rows.items()):
                if held in self.rows and held.get("status")=="PREOWNED_CHILD_TABLE":
                    held["status"]="NO_RETURN"
                    held["cancellation_acknowledged"]=True
                    if rows.get(slot) is held:rows.pop(slot)
        for held in self.rows:
            if held.get("status")=="PREOWNED_CHILD_TABLE":
                held["status"]="NO_RETURN"
                held["cancellation_acknowledged"]=True
    def adopt(self):
        # pid is the real current child, not a completion/authority marker.
        self.pid=os.getpid()
        if os.getppid()!=self.parent_pid:raise Refused("child_table_parent")
        if (self.pid==self.parent_pid or self.state is None
                or self.state.journals!=[self] or self.state.count!=len(self.rows)
                or self.state.row_ids!={id(row) for row in self.rows}
                or any(type(row.get('generation')) is not int for row in self.rows)):
            raise Refused('prepared_actual_child_domain_prefix_CODE','terminal')
        # Only the fork's private copy changes domain. Root _owned() remains
        # guarded by the original parent PID; no inherited Root meter call.
        self.observer.fd_state=self.state
        for child,original in self.parent_rows:
            original['status']='PARENT_SNAPSHOT_TRANSFERRED_TO_CHILD_TABLE'
            self.grants[child['credit']].fd_rows[child['slot']]=child
        for r in self.rows:
            if r['status']=='PREOWNED_CHILD_TABLE':
                r['status']='HELD'
        # A failed existing-field transition retains every preowned cell in the
        # complete primary domain with the actual occupied inherited descriptor.
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
    def record_parent_table_end(self,pid,status):
        """Cancel only this Root copy's prospective child cells after real wait4.

        Never close an inherited numeric FD or mutate the accepted child packet.
        Child termination is not full native body/alias/outside-end acceptance.
        """
        if (os.getpid()!=self.parent_pid or self.pid is not None
                or type(pid) is not int or pid<=0 or type(status) is not int
                or self.parent_child_pid!=pid):
            raise Refused('parent_child_table_end_owner','terminal')
        self.observer._owned()
        waits=[row for row in self.observer.waits
            if row['pid']==pid and row['raw_wait_status']==status]
        if len(waits)!=1:raise Refused('parent_child_table_actual_wait4','terminal')
        if self.parent_table_end is not None:
            if self.parent_table_end is not waits[0]:
                raise Refused('parent_child_table_end_identity','terminal')
            # A prior partial cancellation is not successful completion and is
            # never repeated. Keep all actual surviving rows/credits visible.
            if (any(row['status']=='PREOWNED_CHILD_TABLE' for row in self.rows)
                    or any(getattr(self.grants.get(row['credit']),'fd_rows',{}).get(row['slot']) is row
                        for row in self.rows)):
                raise Refused('parent_child_table_end_partial_UNCONFIRMED','terminal')
            return
        self.parent_table_end=waits[0]
        for row in self.rows:
            if row['status']=='PREOWNED_CHILD_TABLE':
                row['status']='PARENT_PLAN_CANCELLED_CHILD_REAPED'
                row['cancellation_acknowledged']=True
            credit=self.grants.get(row['credit'])
            if credit is not None and getattr(credit,'fd_rows',{}).get(row['slot']) is row:
                credit.fd_rows.pop(row['slot'])
    def graph(self):
        rows=[{k:v for k,v in r.items() if k not in ('journal','close_cell')} for r in self.rows]
        credits=[{'token':c.token,'slots':c.slots,'closed':c.closed} for c in self.grants.values()]
        return {'owner_pid':self.pid,'parent_pid':self.parent_pid,'history_codec':encode_history_rows(rows),
            'journal_count':len(rows),'pending_fds':sorted(self.fds),'fault_count':len(self.faults),
            'fault_chunks':_history_chunks(self.faults),'truncated':False,
            'credit_count':len(credits),'credit_chunks':_history_chunks(credits),
            'collection':collection_for(rows,self.state,id(self.rows))}

    def add(self,value):
        """Actual full bytes on existing fork channel; no inherited Root calls."""
        from common import DOCUMENT_MAX,integer
        import struct
        if self.body_binding is None or self.body_fd<0:raise Refused('prepared_fork_body_binding','terminal')
        if type(value) not in (bytes,bytearray):raise Refused('prepared_fork_body_type','terminal')
        integer(len(value),DOCUMENT_MAX)
        total=self.body_count+len(value)
        transport=total+8*((total+65535)//65536)
        b=self.body_binding
        if (3*total+transport>b['reads'] or total+transport>b['output']
                or 2*total>b['hash_bytes'] or 6*total+131072>b['allocation']):
            raise Refused('prepared_fork_body_existing_credit_before_materialization','terminal')
        snapshot=value if type(value) is bytes else bytes(value)
        offset=self.body_count
        for at in range(0,len(snapshot),65536):
            part=snapshot[at:at+65536]
            for blob in (struct.pack('>Q',len(part)|(1<<63)),part):
                sent=0
                while sent<len(blob):
                    n=os.write(self.body_fd,memoryview(blob)[sent:])
                    if n<=0:raise Refused('prepared_fork_body_short','terminal')
                    sent+=n;self.body_transport+=n
            self.body_count+=len(part)
        self.body_aliases.append((value,snapshot))
        return {'offset':offset,'bytes':len(snapshot)}
    def finish(self):
        if self.body_finished:raise Refused('prepared_fork_body_finish_once','terminal')
        self.body_finished=True
        for value,snapshot in self.body_aliases:
            if len(value)!=len(snapshot) or memoryview(value)!=memoryview(snapshot):
                raise Refused('prepared_fork_body_actual_mutable_drift','terminal')
        return {'endpoint':self.body_binding['endpoint'],'bytes':self.body_count}
    def require_body_ack(self):
        if self.body_ack_fd is None or self.body_deadline is None:
            raise Refused('prepared_fork_body_actual_ack_binding','terminal')
        # Failure can precede the ordinary G/A reads. Drain only their exact
        # original sequence; neither G nor A is physical-body acceptance.
        for _ in range(3):
            left=(self.body_deadline-mono())/1e9
            if left<=0:raise Refused('prepared_fork_body_ack_deadline','terminal')
            with selectors.PollSelector() as selected:
                selected.register(self.body_ack_fd,selectors.EVENT_READ)
                if not selected.select(left):raise Refused('prepared_fork_body_ack_deadline','terminal')
            byte=os.read(self.body_ack_fd,1)
            if byte==b'G' and not self.body_gate_seen:
                self.body_gate_seen=True
            elif byte==b'A' and self.body_gate_seen and not self.body_owner_ack_seen:
                self.body_owner_ack_seen=True
            elif byte==b'B':return
            else:raise Refused('prepared_fork_body_reader_unconfirmed','terminal')
        raise Refused('prepared_fork_body_reader_unconfirmed','terminal')

    def emit(self,fd):
        if self.export_attempted:raise Refused('child_owner_export_already_attempted','terminal')
        self.export_attempted=True
        from common import canonical,INPUT_MAX
        import struct
        self.body_fd=fd
        from observer import full_value_arena
        values=full_value_arena([self.rows,self.state],history_states=(self.state,),body_carrier=self)
        graph={'schema':'friday.sol090.fork-owner-full.v3','owner_pid':self.pid,'parent_pid':self.parent_pid,
            'inherited':{'collection_identity':str(id(self.rows))},
            'new_journals':self.state.graph(),'delivery_fd':fd,'truncated':False,
            'values':values}
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
        self.body_fd=fd
        if self.failure_mailbox is not None:
            return self._emit_failure_preowned_bank(error)
        if self.state is None:
            self.failure_errors.append(error)
            raise Refused('native_child_domain_constructor_before_state_CODE','terminal')
        values=full_value_arena([error,{'collection_identity':str(id(self.rows))},
            None if self.state is None else {'domain_identity':str(id(self.state))}],
            history_states=() if self.state is None else (self.state,),body_carrier=self)
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
        self.require_body_ack()

def _receive_fork_owner_wire(fd,hold,deadline,pid,parent_pid,final_failure=False,allow_eof=False,body_reader=None,ack_fd=None,failure_owner=None):
    """Only prepaid fixed framing/parse; not ordinary deadline admission."""
    from common import parse,INPUT_MAX,json_preflight
    import struct
    def receive(n,charge=True):
        data=bytearray(n);at=0
        while at<n:
            if failure_owner is not None:
                packet=receive_preowned_fork_failure(failure_owner,pid)
                if packet is not None:
                    if ack_fd is None or os.write(ack_fd,b'B')!=1:
                        raise Refused('prepared_fork_body_actual_ack','terminal')
                    raise _PreownedFailureReady(packet)
            left=(deadline-mono())/1e9
            if left<=0:raise Refused('child_owner_deadline','terminal')
            with selectors.PollSelector() as selected:
                selected.register(fd,selectors.EVENT_READ)
                if not selected.select(min(left,0.1)):
                    continue
            part=os.read(fd,min(65536,n-at))
            if not part:
                if allow_eof and at==0 and n==8:return None
                raise Refused('child_owner_eof','terminal')
            data[at:at+len(part)]=part;at+=len(part)
            if charge:hold.commit(reads=len(part))
        return bytes(data)
    header=receive(8)
    if header is None:return None
    n=struct.unpack('>Q',header)[0]
    while n&(1<<63):
        width=n&((1<<63)-1)
        if body_reader is None or not 1<=width<=65536:
            raise Refused('prepared_fork_body_chunk','terminal')
        body_reader.prospective(width,width+8)
        part=receive(width,charge=False)
        body_reader.append(part,transport=width+8)
        header=receive(8)
        if header is None:raise Refused('prepared_fork_body_no_metadata','terminal')
        n=struct.unpack('>Q',header)[0]
    if n>INPUT_MAX:raise Refused('child_owner_capacity','terminal')
    raw=receive(n);value=parse(raw,maximum=INPUT_MAX)
    failure=value.get('schema')=='friday.a190.fork-final-failure.v1'
    schema='friday.a190.fork-final-failure.v1' if final_failure or failure else value.get('schema')
    if not (final_failure or failure) and schema not in ('friday.a181.fork-owner-graph.v2','friday.sol090.fork-owner-full.v3'):
        raise Refused('child_owner_identity','terminal')
    if value.get('schema')!=schema or value.get('owner_pid')!=pid or value.get('parent_pid')!=parent_pid or value.get('truncated') is not False:
        raise Refused('child_owner_identity','terminal')
    if final_failure or failure:
        # BOTH native and Root callers enter this same receiver before they
        # accept/classify a failure graph. Packet shape or complete=True alone
        # is not nested full-body/index/alias validation or native custody.
        from common import exact
        from observer import validate_full_value_arena
        exact(value,('schema','owner_pid','parent_pid','phase','values','error',
                     'complete','truncated'),'child_final_failure_schema')
        if (value['phase']!='FINAL_FAILURE' or type(value['values']) is not dict
                or value['complete'] is not True
                or type(value['owner_pid']) is not int
                or type(value['parent_pid']) is not int):
            raise Refused('child_final_failure_schema','terminal')
        if body_reader is None:raise Refused('prepared_fork_full_reader_required','terminal')
        body_reader.finish()
        nodes=validate_full_value_arena(value['values'],body_reader)
        if type(value['values']['roots']) is not list or len(value['values']['roots'])!=3:
            raise Refused('child_final_failure_roots','terminal')
        from observer import full_value_literal, validate_fd_value_histories
        roots=value['values']['roots']
        reference=full_value_literal(nodes,roots[1])
        domain_ref=full_value_literal(nodes,roots[2])
        from observer import full_value_history_domain
        domain=full_value_history_domain(value['values'],domain_ref)
        fork_history_book({'schema':'friday.a181.fork-owner-graph.v2',
            'owner_pid':pid,'parent_pid':parent_pid,'inherited':reference,'new_journals':domain})
        # validate_full_value_arena already joins every actual collection to
        # the single codec bank, including original inherited/error aliases.
    else:
        from common import exact
        fields=('schema','owner_pid','parent_pid','inherited','new_journals','delivery_fd','truncated')
        if schema=='friday.sol090.fork-owner-full.v3':
            fields=fields+('values',)
            from observer import validate_full_value_arena,full_value_history_domain
            if body_reader is None:raise Refused('prepared_fork_full_reader_required','terminal')
            body_reader.finish()
            validate_full_value_arena(value['values'],body_reader)
            if len(value['values']['roots'])!=2:raise Refused('ordinary_actual_fork_roots','terminal')
            actual=full_value_history_domain(value['values'],{'domain_identity':value['new_journals']['domain_identity']})
            if actual!=value['new_journals']:raise Refused('ordinary_actual_fork_history_join','terminal')
        exact(value,fields,'child_owner_schema')
        fork_history_book(value)
    hold.commit(output=n+8)
    value['_receiver_resident_upper']=json_preflight(raw)+len(raw)*4+131072
    if failure or final_failure:
        if ack_fd is None or os.write(ack_fd,b'B')!=1:raise Refused('prepared_fork_body_actual_ack','terminal')
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

def _fork_failure_preowned_bank(self,error):
    """Actual full failure body survives a broken ordinary pipe in parent FD."""
    from observer import full_value_arena,validate_full_value_arena
    from common import canonical,INPUT_MAX
    bank=self.failure_mailbox
    if bank is None:raise Refused('actual_preowned_native_failure_bank_required','terminal')
    bank.bind_child()
    self.failure_errors.append(error)
    values=full_value_arena([error,{'collection_identity':str(id(self.rows))},
        {'domain_identity':str(id(self.state))}],history_states=(self.state,),body_carrier=bank)
    # Same exact eight-field original failure schema/three actual roots.
    graph={'schema':'friday.a190.fork-final-failure.v1','owner_pid':os.getpid(),
        'parent_pid':self.parent_pid,'phase':'FINAL_FAILURE','values':values,
        'error':error_fact(error,'terminal'),'complete':values['complete'],'truncated':False}
    self.failure_supply=bank.publish(graph)
    # Same original G/A/B gate. Root's full reader writes B only after acceptance.
    self.require_body_ack()
    if not bank.accepted_by_parent():
        raise Refused('native_child_failure_bank_acceptance_end_CODE','terminal')
    # No exit/FD/hash/true-bit is receiver acceptance. The current parent must
    # do the full reader/alias/history validation on BOTH actual endpoints.
    return self.failure_supply
ForkOwner._emit_failure_preowned_bank=_fork_failure_preowned_bank

def receive_preowned_fork_failure(owner,pid):
    bank=getattr(owner,'failure_mailbox',None)
    if bank is None or bank.accepted is not None:return None
    packet=bank.read_parent(pid)
    if packet is None:return None
    exact(packet,('schema','owner_pid','parent_pid','phase','values','error','complete','truncated'),
        'child_final_failure_schema')
    if (packet['schema']!='friday.a190.fork-final-failure.v1' or packet['parent_pid']!=os.getpid()
            or packet['phase']!='FINAL_FAILURE' or packet['complete'] is not True or packet['truncated'] is not False):
        raise Refused('child_final_failure_schema','terminal')
    from observer import validate_full_value_arena,full_value_literal,full_value_history_domain
    nodes=validate_full_value_arena(packet['values'],bank)
    roots=packet['values']['roots']
    if len(roots)!=3:raise Refused('child_final_failure_roots','terminal')
    reference=full_value_literal(nodes,roots[1]);domain_ref=full_value_literal(nodes,roots[2])
    domain=full_value_history_domain(packet['values'],domain_ref)
    fork_history_book({'schema':'friday.a181.fork-owner-graph.v2','owner_pid':pid,
        'parent_pid':os.getpid(),'inherited':reference,'new_journals':domain})
    bank.acknowledge(pid,packet)
    return packet

class _PreownedFailureReady(BaseException):
    def __init__(self,packet):self.packet=packet

def receive_fork_owner(fd,hold,deadline,pid,parent_pid,final_failure=False,allow_eof=False,body_reader=None,ack_fd=None,failure_owner=None):
    try:
        return _receive_fork_owner_wire(fd,hold,deadline,pid,parent_pid,final_failure,
            allow_eof,body_reader,ack_fd,failure_owner)
    except _PreownedFailureReady as supplied:
        return supplied.packet
