"""Inert receipt adjunct. Vector bytes are copied before the call. Flush adds none. The author does not run this module.
"""
def extend_graph(module):
    owner=module.SourceGraphOwner
    original=owner._invoke
    def _invoke(self,operation,function,args,kwargs):
        if operation not in ("os.writev","os.pwritev","stream.flush","raw.flush"):
            return original(self,operation,function,args,kwargs)
        payload=b""
        if operation in ("os.writev","os.pwritev"):
            buffers=args[1] if len(args)>1 else kwargs.get("buffers") or ()
            payload=b"".join(bytes(memoryview(buf)) for buf in buffers)
        real=self.record
        def record(slot):
            channel=slot.setdefault("channel",{})
            if operation in ("os.writev","os.pwritev"):
                channel["physical_payload_before"]=payload
                channel["physical_kind"]="vector_owned_before_call"
                if channel.get("fd") is None:
                    channel["fd"]=args[0] if args else kwargs.get("fd")
                if "identity9" not in channel and channel.get("fd") is not None:
                    channel["identity9"]=module.id9(module._SOURCE_RAW_OS.fstat(channel["fd"]))
                    channel["pipe_buf"]=module._SOURCE_RAW_OS.fpathconf(channel["fd"],"PC_PIPE_BUF") if module.stat.S_ISFIFO(channel["identity9"][2]) else None
                if "physical_sequence" not in channel:
                    channel["physical_sequence"]=self.raw_call_serial
                    self.raw_call_serial+=1
                if slot.get("raw_error") is not None:
                    channel["failed_prefix_count"]=slot.get("raw_result")
            else:
                channel["physical_kind"]="flush_no_new_owned_bytes"
            return real(slot)
        self.record=record
        try:
            return original(self,operation,function,args,kwargs)
        finally:
            self.record=real
    owner._invoke=_invoke
