"""NEW future Root-owned launcher/performer child. No current invocation."""
import os
import struct
from common import Refused, INPUT_MAX, parse, canonical, encoded_bound, error_fact, mono
from observer import MeterRPC
from actor_context import ActorContext
from operations import PerformingOperations
from admission import ALL15
from consumer_bridge import OrdinaryInput


def retained_consumer(channel,admission,fact,ordinary,consumer,meter):
    """Consume independently retained historical Root facts, not fresh oracle.

    The selected physical package was verified by the actual Root parent before
    launching this new actor. Source neither copies observations into expected
    nor edits immutable A128 schemas, cap/hash domains or planner contracts.
    """
    selected=OrdinaryInput(ordinary,admission["inputs"],meter,consumer,fact["actor_id"])
    catalog={r["id"]:r for r in admission["coverage"]["all69"]}
    if selected.selected["case_id"] in catalog:
        output=selected.invoke_control(catalog[selected.selected["case_id"]])
    else:output=selected.invoke()
    from custody import OutputStore
    store=OutputStore(admission["output_root"]+"/actor",meter)
    try:
        record=store.put("full-consumer-output",output,"member")
        channel.call({"type":"done","result_pin":record["pin"],"all15":list(ALL15),"binding":selected.full_binding()})
    finally:store.close()


class ControlChannel:
    """Root creates both endpoints. Channel bytes confer no Root authority."""
    def __init__(self,outgoing,incoming,child_journal=None,owner_export_token=None):
        self.outgoing,self.incoming=outgoing,incoming
        self.delivered=0
        self.meter=None;self.transport_hold=None;self.accounting=False
        self.child_journal=child_journal;self.owner_export_token=owner_export_token
        self.body_binding=None;self.body_count=0;self.body_aliases=[];self.body_finished=False

    def body_call(self,request):
        return self.terminal_call(dict(request,owner_pid=os.getpid(),
            prepaid_token=self.owner_export_token))
    def body_begin(self):
        if self.body_binding is None:
            self.body_binding=self.body_call({'type':'owner-body-begin'})
        return self.body_binding

    def add(self, value):
        if type(value) not in (bytes,bytearray):raise RuntimeError('prepared-body-type')
        binding=self.body_begin();total=self.body_count+len(value)
        wire=2*total+((total+32767)//32768)*512+1024
        # Every actual byte/copy/chunk/RPC/read/hash is nonzero. These limits
        # are the existing prepaid pools, not lower original body/case caps.
        scan_reads=getattr(self,'secondary_scan_reads',0)
        scan_alloc=getattr(self,'secondary_scan_allocation',0)
        if (type(scan_reads) is not int or type(scan_alloc) is not int
                or isinstance(scan_reads,bool) or isinstance(scan_alloc,bool)
                or scan_reads<0 or scan_alloc<0
                or scan_reads>binding['reads'] or scan_alloc>binding['allocation']
                or 4*total>binding['reads']-scan_reads or 2*total>binding['output']
                or 2*total>binding['hash_bytes']
                or 6*total+131072>binding['allocation']-scan_alloc
                or wire>binding['wire_reads'] or wire>binding['wire_output']):
            raise RuntimeError('prepared-body-existing-credit-before-materialization')
        snapshot=value if type(value) is bytes else bytes(value)
        offset=self.body_count
        for at in range(0,len(snapshot),32768):
            part=snapshot[at:at+32768]
            reply=self.body_call({'type':'owner-body-chunk','offset':self.body_count,'body':part.hex()})
            self.body_count+=len(part)
            if reply!={'bytes':self.body_count}:raise RuntimeError('prepared-body-Root-count')
        self.body_aliases.append((value,snapshot))
        return {'offset':offset,'bytes':len(snapshot)}

    def finish(self):
        if self.body_finished:raise RuntimeError('prepared-body-finish-once')
        binding=self.body_begin()
        for value,snapshot in self.body_aliases:
            if len(value)!=len(snapshot) or memoryview(value)!=memoryview(snapshot):
                raise RuntimeError('prepared-body-actual-mutable-drift')
        self.body_finished=True
        result=self.body_call({'type':'owner-body-end','bytes':self.body_count})
        if result!={'endpoint':binding['endpoint'],'bytes':self.body_count}:
            raise RuntimeError('prepared-body-Root-descriptor')
        return result

    def configure(self,meter):
        if self.meter is not None:raise Refused("channel_duplicate_owner")
        # The small recursive accounting exchange has separate prospective
        # credit. Escaping parsed replies stay charged until Root confirms the
        # actual actor terminal, never until just the response frame returns.
        self.transport_hold=meter.reserve("actor-control-escaped-return-lifetime",allocation=1048576)
        self.meter=meter
    def before_allocation(self,size):
        if self.meter is None:return
        if self.accounting:
            if size>1048576:raise Refused("accounting_recursion_capacity")
            return
        self.accounting=True
        try:self.meter.grow(self.transport_hold.token,allocation=size)
        finally:self.accounting=False
    def write(self,raw,prepaid=False):
        if len(raw)>INPUT_MAX:raise Refused("control_capacity")
        for part in (struct.pack(">Q",len(raw)|((1<<63) if prepaid else 0)),raw):
            at=0
            while at<len(part):
                n=os.write(self.outgoing,memoryview(part)[at:])
                if n<=0:raise Refused("control_delivery","after-delivery")
                at+=n;self.delivered+=n
    def read(self):
        def receive(size):
            out=bytearray()
            while len(out)<size:
                part=os.read(self.incoming,min(65536,size-len(out)))
                if not part:raise Refused("control_eof","after-delivery")
                out.extend(part)
            return bytes(out)
        n=struct.unpack(">Q",receive(8))[0]
        prepaid=bool(n&(1<<63));n&=(1<<63)-1
        if n>INPUT_MAX:raise Refused("control_capacity")
        if prepaid:
            if self.owner_export_token is None:raise Refused("owner_export_not_preadmitted","terminal")
        else:self.before_allocation(n*260+65536)
        return parse(receive(n))
    def call(self,request):
        self.before_allocation(encoded_bound(request)*4+65536)
        self.write(canonical(request))
        response=self.read()
        if response["ok"] is not True:
            error=response["error"]
            raise Refused(error["cause"],error["phase"],error)
        return response["value"]
    def terminal_call(self,request):
        if self.owner_export_token is None:raise Refused("owner_export_not_preadmitted","terminal")
        self.write(canonical(request),prepaid=True)
        response=self.read()
        if response["ok"] is not True:
            error=response["error"];raise Refused(error["cause"],error["phase"],error)
        return response["value"]
    def close(self):
        # Parent owns the transport reservation until wait4-confirmed terminal.
        # No RPC release can shrink a lifetime while parsed reply aliases live.
        if self.child_journal is None:raise Refused("channel_child_journal","terminal")
        for fd in (self.incoming,self.outgoing):self.child_journal.close_one(fd)


def actor_main(outgoing,incoming,admission,unused_fork_fact,ordinary,consumer,
               initial_launch=None,existing_channel=None,existing_meter=None):
    channel=existing_channel or ControlChannel(outgoing,incoming)
    # This is an actual launch fact sent by the parent AFTER Root creates and
    # observes the pidfd/process/cgroup/pipe graph; fork-time None is not evidence.
    fact=initial_launch or channel.read()["launch_fact"]
    if fact["pid"]!=os.getpid() or fact["actual_parent"]["pid"]!=os.getppid():
        raise Refused("actual_child_parent")
    # The loader, consumer callback, transport and all operation owners share
    # the SAME actor meter and monotonically published graph. A second meter
    # would reset the sequence and replace the loader graph with a partial one.
    meter=existing_meter or MeterRPC(channel.call,channel.owner_export_token,channel.terminal_call)
    if getattr(meter,'owner_body_carrier',None) is None:meter.owner_body_carrier=channel
    if channel.meter is None:channel.configure(meter)
    context=None
    try:
        if admission["launch_mode"]=="retained-consumer":
            retained_consumer(channel,admission,fact,ordinary,consumer,meter)
            return
        context=ActorContext(admission,fact,meter,channel.call,consumer,ordinary)
        ops=PerformingOperations(context,context.expected)
        ops.acquire_material_phase()
        for spec in context.expected["operations"]:
            predecessors=[p for p in context.predecessors if p["name"] in spec["dependencies"]]
            actual=consumer("performing_contracts").operation_input(spec["name"],context.expected,
                context.refresh_composition(),context.ordinary.presented,predecessors)
            ops.run(spec["name"],actual)
        context.retain_final_domains(ops.outcome(ops.members))
        # Complete actual public predecessor preimages are retained BEFORE a
        # different Root owner selects the final five arguments and whole
        # golden. This is an observation ledger, not an expected-output builder.
        from common import OUTPUT_MAX
        preimage=context.store.put("full-produced-public-preimages",
            canonical(context.predecessors,meter,OUTPUT_MAX),"member")
        selected=channel.call({"type":"final-independent-ordinary","public_preimage_pin":preimage["pin"]})
        final=OrdinaryInput(selected["ordinary"],selected["sources"],meter,consumer,fact["actor_id"])
        # The independently selected final package must preserve every fresh
        # actual full operation body. No old bytes/results transfer to this job.
        catalog={r["id"]:r for r in admission["coverage"]["all69"]}
        result=final.invoke_control(catalog[final.selected["case_id"]]) if final.selected["case_id"] in catalog else final.invoke()
        record=context.store.put("full-consumer-output",result,"member")
        channel.call({"type":"done","result_pin":record["pin"],"all15":list(ALL15),"binding":final.full_binding()})
    except BaseException as exc:
        try:channel.call({"type":"failure","name":getattr(context,"operation","preparation"),
                          "error":error_fact(exc,"execution",channel.delivered)})
        except BaseException:pass
        raise
    finally:
        import sys
        primary=sys.exc_info()[1]
        if primary is not None:meter.retain_error_arena(primary)
        try:
            if context is not None:context.close()
            # The stock bootstrap still owns consumer-loader and bundle rows.
            # Its one final export follows those close attempts; publishing
            # here would acknowledge an obsolete prefix of the same graph.
            if existing_channel is None:meter.publish_local_owners(terminal=True)
        except BaseException as export_error:
            if primary is None:raise
            primary.owner_export_error=error_fact(export_error,"terminal")
        # Accepted final transport rows remain HELD until Root confirms this
        # child descriptor-table boundary. A late local close after the sole
        # accepted graph would lose its attempted-close facts.
