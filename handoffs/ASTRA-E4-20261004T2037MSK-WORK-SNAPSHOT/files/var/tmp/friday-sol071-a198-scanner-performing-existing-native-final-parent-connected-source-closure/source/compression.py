"""Bounded stock codec adapters. Policy/API mismatch is explicitly unsupported."""

from .bounds import charge, reserve, release_live, performing_requested
from .pins import CODEC_PINS
from .digests import DigestStop


class BudgetStop(Exception):
    def __init__(self, cause):
        self.cause = cause


def find_capability(capabilities, method, implementation):
    if not isinstance(capabilities, list):
        return None, "compression_capability_absent"
    matches = [item for item in capabilities if isinstance(item, dict) and item.get("method") == method]
    if len(matches) != 1:
        return None, "compression_capability_absent"
    item = matches[0]
    pin = CODEC_PINS.get(method)
    if not isinstance(pin, dict) or item.get("admitted") is not True or item.get("implementation") != implementation:
        return None, "compression_capability_absent"
    if item.get("capability_sha256") != pin["capability_sha256"]:
        return None, "codec_capability_unpinned"
    return item, None


def _feed_chunks(decoder,payload,bound,protocol,resources,meter):
    """Pull <=64KiB decoded chunks; no pieces list, full output or join."""
    from .custody import HeldRange
    if not isinstance(bound,int) or isinstance(bound,bool) or bound<0:
        raise BudgetStop("resource_ceiling_exceeded")
    tick=reserve(resources,meter,live_bytes=131072)
    if tick: raise BudgetStop(tick)
    used=offset=0
    pending=b""
    piece=None
    try:
        while True:
            if decoder.eof:
                if pending or offset!=len(payload) or decoder.unused_data:
                    raise BudgetStop("compressed_stream_malformed")
                return
            if protocol=="zlib.decompressobj.v1":
                pending=decoder.unconsumed_tail or pending
                needs_input=not pending
            else:
                needs_input=decoder.needs_input
            owned_input=0
            if needs_input and not pending:
                if offset==len(payload):
                    if protocol!="zlib.decompressobj.v1":
                        raise BudgetStop("compressed_stream_malformed")
                    pending=b""
                else:
                    wanted=min(65536,len(payload)-offset)
                    pending=payload.read_part(offset,wanted) if isinstance(payload,HeldRange) else memoryview(payload)[offset:offset+wanted]
                    owned_input=wanted+32 if isinstance(payload,HeldRange) else 0
                    offset+=len(pending)
            elif not needs_input and protocol!="zlib.decompressobj.v1":
                pending=b""
            maximum=min(65536,bound-used+1)
            ceilings=resources.get("ceilings") if isinstance(resources,dict) else None
            if isinstance(ceilings,dict) and isinstance(meter,dict):
                maximum=min(maximum,max(1,ceilings["max_expanded_bytes"]-meter.get("expanded_bytes",0)+1))
            tick=reserve(resources,meter,work_bytes=len(pending)+maximum,live_bytes=maximum+32)
            if tick:
                if owned_input:
                    meter["range_return_live"]-=owned_input
                    release_live(meter,owned_input)
                raise BudgetStop(tick)
            before_pending=len(pending)
            try:
                piece=decoder.decompress(pending,max_length=maximum)
                pending=decoder.unconsumed_tail if protocol=="zlib.decompressobj.v1" else b""
                if used+len(piece)>bound: raise BudgetStop("resource_ceiling_exceeded")
                tick=charge(resources,meter,expanded_bytes=len(piece))
                if tick: raise BudgetStop(tick)
                used+=len(piece)
                if piece: yield piece
                if not piece and not decoder.eof and before_pending==0:
                    if protocol!="zlib.decompressobj.v1" and decoder.needs_input and offset<len(payload):
                        continue
                    raise BudgetStop("compressed_stream_malformed")
                if protocol=="zlib.decompressobj.v1" and not piece and len(pending)==before_pending and pending:
                    raise BudgetStop("codec_capability_unsupported")
            finally:
                piece=None
                release_live(meter,maximum+32)
                if owned_input:
                    meter["range_return_live"]-=owned_input
                    release_live(meter,owned_input)
    finally:
        pending=b""
        release_live(meter,131072)


class DecodedReader:
    """One native/pending/output owner, explicitly closed on EVERY branch.
    Sequential TAR bodies are reduced to bounded hash/format state; only the
    original <=1MiB metadata/extension domain can retain complete body bytes.
    """
    def __init__(self,chunks,resources,meter,keep_control=False):
        self.chunks=iter(chunks)
        self.resources,self.meter=resources,meter
        self.piece=b""; self.offset=0; self.eof=False
        self.keep_control=keep_control

    def _fill(self):
        if self.offset<len(self.piece): return True
        self.piece=b""; self.offset=0
        if self.eof: return False
        try: self.piece=next(self.chunks)
        except StopIteration:
            self.eof=True
            return False
        if not isinstance(self.piece,(bytes,bytearray,memoryview)) or not 0<len(self.piece)<=65536:
            raise BudgetStop("compressed_stream_malformed")
        return True

    def read_exact(self,size):
        if not 0<=size<=1048576: raise BudgetStop("resource_ceiling_exceeded")
        tick=reserve(self.resources,self.meter,live_bytes=size*3+256,work_bytes=size*2)
        if tick: raise BudgetStop(tick)
        out=None
        returned=None
        try:
            out=bytearray()
            while len(out)<size and self._fill():
                take=min(size-len(out),len(self.piece)-self.offset)
                out.extend(memoryview(self.piece)[self.offset:self.offset+take]); self.offset+=take
            returned=bytes(out)
            return returned
        finally:
            out=None
            # Preserve only the escaping bytes; retire construction scratch on
            # both short-read and exceptional exits after destroying it.
            release_live(self.meter,size*3+256-(len(returned) if returned is not None else 0))

    def observe(self,size,keep=False,zlib=None):
        import hashlib,base64
        if size<0 or keep and size>1048576: raise BudgetStop("resource_ceiling_exceeded")
        tick=reserve(self.resources,self.meter,live_bytes=12288+(size*3+256 if keep else 0),work_bytes=12288)
        if tick: raise BudgetStop(tick)
        states={}
        out=None
        remaining=size; crc=0
        returned=None
        try:
            states={name:hashlib.new(name) for name in ("sha256","sha384","sha512")}
            out=bytearray() if keep else None
            while remaining:
                if not self._fill(): raise BudgetStop("truncated_tar_member")
                take=min(remaining,len(self.piece)-self.offset)
                tick=reserve(self.resources,self.meter,live_bytes=take+32,work_bytes=take*4)
                if tick: raise BudgetStop(tick)
                try:
                    part=memoryview(self.piece)[self.offset:self.offset+take]
                    for state in states.values(): state.update(part)
                    if zlib is not None: crc=zlib.crc32(part,crc)
                    if keep: out.extend(part)
                    self.offset+=take; remaining-=take
                finally:
                    part=None
                    release_live(self.meter,take+32)
            tick=reserve(self.resources,self.meter,live_bytes=2048,work_bytes=2048)
            if tick: raise BudgetStop(tick)
            facts={"size":size,"sha256":states["sha256"].hexdigest(),
                "record_digests":{name:base64.urlsafe_b64encode(state.digest()).rstrip(b"=").decode("ascii") for name,state in states.items()},
                "crc32":crc & 0xffffffff if zlib is not None else None}
            returned=bytes(out) if keep else b""
            return returned,facts
        finally:
            out=None
            states.clear()
            release_live(self.meter,12288)
            if keep:
                release_live(self.meter,size*3+256-(len(returned) if returned is not None else 0))

    def zero_tail(self):
        count=0
        while self._fill():
            tail=memoryview(self.piece)[self.offset:]
            tick=charge(self.resources,self.meter,work_bytes=len(tail))
            if tick: raise BudgetStop(tick)
            if any(byte for byte in tail): return False
            count+=len(tail); self.offset=len(self.piece); tail=None
        self.tail_zero_bytes=count
        return count%512==0

    def close(self):
        self.piece=b""; self.offset=0
        closer=getattr(self.chunks,"close",None)
        if closer is not None: closer()


def _stored_chunks(payload,resources,meter):
    from .digests import _chunks
    chunks=_chunks(payload,resources,meter)
    try:
        for piece in chunks:
            tick=charge(resources,meter,expanded_bytes=len(piece))
            if tick: raise BudgetStop(tick)
            yield piece
    finally:
        piece=None
        chunks.close()


def stream_reader(method,payload,capabilities,limit,resources,meter,keep_control=False):
    chunks=_stored_chunks(payload,resources,meter) if method is None else _decode_chunks_admitted(method,payload,capabilities,limit,resources,meter)
    return DecodedReader(chunks,resources,meter,keep_control)


def decode_admitted(method,payload,capabilities,limit,resources=None,meter=None):
    """Legacy finite DATA API, not the performing archive body consumer.
    The performing ZIP/DEB paths use stream_reader and never retain this buffer.
    """
    tick=reserve(resources,meter,live_bytes=4096,work_bytes=1024)
    if tick: return None,tick
    out=None
    chunks=None
    buffer_live=0
    returned_live=0
    returned=None
    try:
        out=bytearray()
        chunks=_decode_chunks_admitted(method,payload,capabilities,limit,resources,meter)
        for piece in chunks:
            tick=reserve(resources,meter,live_bytes=len(piece)+32,work_bytes=len(piece))
            if tick: raise BudgetStop(tick)
            buffer_live+=len(piece)+32
            out.extend(piece)
        tick=reserve(resources,meter,live_bytes=len(out)+32)
        if tick: raise BudgetStop(tick)
        returned_live=len(out)+32
        returned=bytes(out)
        return returned,None
    except BudgetStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,stop)
        from .causes import source_exception_detail
        if isinstance(meter,dict):meter.setdefault('origin_failures',[]).append(source_exception_detail(meter,stop))
        return None,stop.cause
    finally:
        piece=None
        out=None
        try:
            if chunks is not None: chunks.close()
        finally:
            chunks=None
            release_live(meter,buffer_live)
            release_live(meter,4096)
            if returned is None: release_live(meter,returned_live)


def _decode_chunks_admitted(method, payload, capabilities, limit, resources=None, meter=None):
    from .causes import enter_phase
    phase = enter_phase(meter,"SHARED_STREAM_AND_CODEC","decode_admitted",method)
    if phase:
        raise BudgetStop(phase)
    implementations = {"deflate": "stdlib.zlib", "gzip": "stdlib.zlib", "xz": "stdlib.lzma", "zstd": "stdlib.compression.zstd"}
    protocols = {"deflate": "zlib.decompressobj.v1", "gzip": "zlib.decompressobj.v1", "xz": "lzma.LZMADecompressor.v1", "zstd": "zstd.decompressor.windowLogMax.v1"}
    implementation = implementations.get(method)
    if implementation is None:
        raise BudgetStop("compression_method_unsupported")
    item, cause = find_capability(capabilities, method, implementation)
    if cause:
        raise BudgetStop(cause)
    mem = item.get("decoder_memory_bytes")
    if not isinstance(mem, int) or isinstance(mem, bool) or mem < 32768:
        raise BudgetStop("codec_memory_policy_absent")
    if item.get("protocol") != protocols[method]:
        raise BudgetStop("codec_capability_unsupported")
    modules = meter.get("codec_modules") if isinstance(meter, dict) else None
    selected = modules.get(method) if isinstance(modules, dict) else None
    if performing_requested(resources):
        from .contracts import loaded_globals_cause
        graph_cause = loaded_globals_cause(meter)
        if graph_cause:
            raise BudgetStop(graph_cause)
        if not isinstance(selected, dict) or selected.get("protocol") != protocols[method] or meter.get("implementation_bound") is not True:
            raise BudgetStop("codec_capability_unpinned")
        module = selected.get("module")
    else:
        module = None
    evidence = selected.get("allocation_contract") if isinstance(selected,dict) else None
    if performing_requested(resources):
        constructor = {"deflate":"decompressobj(-15)","gzip":"decompressobj(31)",
                       "xz":"LZMADecompressor(FORMAT_XZ,memlimit)","zstd":"ZstdDecompressor(windowLogMax)"}[method]
        pending_api = "unconsumed_tail" if method in ("deflate","gzip") else "needs_input"
        if (not isinstance(evidence,dict) or evidence.get("constructor") != constructor or
            evidence.get("bounded_output_api") != "decompress(max_length)" or
            evidence.get("pending_input_api") != pending_api or evidence.get("eof_api") != "eof" or
            evidence.get("unused_data_api") != "unused_data" or
            evidence.get("method") != method or evidence.get("protocol") != protocols[method] or
            evidence.get("module_sha256") != selected.get("sha256")):
            raise BudgetStop("codec_capability_unpinned")
        state = evidence["state_upper_bound_bytes"]
        window = evidence["max_window_bytes"]
        overhead = evidence["native_overhead_upper_bound_bytes"]
        total = evidence["total_native_upper_bound_bytes"]
        if total != state+window+overhead or total > mem:
            raise BudgetStop("codec_memory_policy_absent")
        if method in ("deflate","gzip") and window != 32768:
            raise BudgetStop("codec_memory_policy_absent")
        if method == "zstd" and window != 1 << item.get("window_log",0):
            raise BudgetStop("codec_memory_policy_absent")
        if method == "xz" and (evidence["constructor_memory_limit_bytes"] != state+window or state+window < 32768):
            raise BudgetStop("codec_memory_policy_absent")
    # One complete selected native/state/window/overhead envelope is reserved
    # BEFORE a constructor allocates.  This is not an audit of those bounds.
    tick = reserve(resources, meter, live_bytes=mem)
    if tick:
        raise BudgetStop(tick)
    try:
        if module is None:
            if method in ("deflate", "gzip"):
                import zlib as module
            elif method == "xz":
                import lzma as module
            else:
                import compression.zstd as module
        if method in ("deflate", "gzip"):
            bound_state = item.get("decoder_state_upper_bound")
            evidence = selected.get("allocation_contract") if isinstance(selected,dict) else None
            if performing_requested(resources) and (not isinstance(evidence,dict) or
                evidence.get("state_upper_bound_bytes") != bound_state or evidence.get("method") != method or
                evidence.get("protocol") != protocols[method] or evidence.get("module_sha256") != selected.get("sha256")):
                raise BudgetStop("codec_capability_unpinned")
            if not isinstance(bound_state, int) or isinstance(bound_state, bool) or bound_state < 32768 or bound_state > mem:
                raise BudgetStop("codec_capability_unsupported")
            decoder = module.decompressobj(-15 if method == "deflate" else 31)
        elif method == "xz":
            constructor_limit = evidence["constructor_memory_limit_bytes"] if performing_requested(resources) else mem
            decoder = module.LZMADecompressor(format=module.FORMAT_XZ, memlimit=constructor_limit)
        else:
            option = getattr(getattr(module, "DecompressionParameter", None), "windowLogMax", None)
            window = item.get("window_log")
            if option is None or not isinstance(window, int) or isinstance(window, bool) or not 10 <= window <= 27 or (1 << window) > mem:
                raise BudgetStop("codec_capability_unsupported")
            try:
                decoder = module.ZstdDecompressor(options={option: window})
            except (TypeError, ValueError, AttributeError) as raw_origin:
                from tools.native_support import retain_source_origin
                retain_source_origin(meter,raw_origin)
                raise BudgetStop("codec_capability_unsupported")
        yield from _feed_chunks(decoder,payload,limit,protocols[method],resources,meter)
        return
    except BudgetStop as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        raise
    except ImportError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        raise BudgetStop({"deflate": "runtime_zlib_module_absent", "gzip": "runtime_zlib_module_absent", "xz": "runtime_lzma_module_absent", "zstd": "runtime_zstd_module_absent"}[method])
    except MemoryError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        raise BudgetStop("resource_ceiling_exceeded")
    except (TypeError, AttributeError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        raise BudgetStop("codec_capability_unsupported")
    except DigestStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,stop)
        raise BudgetStop(stop.cause)
    except Exception as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        raise BudgetStop("compressed_stream_malformed")
    finally:
        decoder=None
        release_live(meter, mem)



def decode_deflate(payload, capabilities, limit, resources=None, meter=None):
    return decode_admitted("deflate", payload, capabilities, limit, resources, meter)


def decode_gzip(payload, capabilities, limit, resources=None, meter=None):
    return decode_admitted("gzip", payload, capabilities, limit, resources, meter)


def decode_xz(payload, capabilities, limit, resources=None, meter=None):
    return decode_admitted("xz", payload, capabilities, limit, resources, meter)


def decode_zstd(payload, capabilities, limit, resources=None, meter=None):
    return decode_admitted("zstd", payload, capabilities, limit, resources, meter)


def reject_external_tool(capabilities):
    for item in capabilities if isinstance(capabilities, list) else []:
        implementation = item.get("implementation") if isinstance(item, dict) else None
        if isinstance(implementation, str) and implementation.startswith(("tool:", "subprocess:")):
            return "compression_external_tool_refused"
    return None


DECODERS = {"deflate": decode_deflate, "gzip": decode_gzip, "xz": decode_xz, "zstd": decode_zstd}
