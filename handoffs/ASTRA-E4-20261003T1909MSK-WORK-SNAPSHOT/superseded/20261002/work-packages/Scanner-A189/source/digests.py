"""Wheel RECORD digest helpers. md5 and sha1 are not accepted as RECORD algorithms."""

SUPPORTED_RECORD_ALGORITHMS = ("sha256", "sha384", "sha512")


class DigestStop(Exception):
    def __init__(self, cause):
        self.cause = cause


def _chunks(data, resources, meter):
    from .bounds import charge,reserve,release_live
    from .custody import HeldRange
    if isinstance(data,HeldRange):
        chunks=data.chunks()
        try:
            for piece in chunks:
                tick=charge(resources,meter,work_bytes=len(piece))
                if tick: raise DigestStop(tick)
                yield piece
        finally:
            piece=None
            chunks.close()
        return
    tick=reserve(resources,meter,live_bytes=4096,work_bytes=1024)
    if tick: raise DigestStop(tick)
    view=piece=None
    try:
        view = memoryview(data)
        for offset in range(0, len(view), 65536):
            piece = view[offset:offset + 65536]
            tick = charge(resources, meter, work_bytes=len(piece))
            if tick:
                raise DigestStop(tick)
            yield piece
    finally:
        piece=None
        view=None
        release_live(meter,4096)


def content_sha256(data, resources=None, meter=None):
    from .bounds import reserve,release_live
    tick=reserve(resources,meter,live_bytes=8192,work_bytes=8192)
    if tick: raise DigestStop(tick)
    digest=chunks=piece=None
    try:
        import hashlib
        digest = hashlib.sha256()
        chunks=_chunks(data, resources, meter)
        for piece in chunks:
            digest.update(piece)
        tick=reserve(resources,meter,live_bytes=512,work_bytes=512)
        if tick: raise DigestStop(tick)
        return digest.hexdigest()
    finally:
        piece=None
        try:
            if chunks is not None: chunks.close()
        finally:
            chunks=None
            digest=None
            release_live(meter,8192)


def record_digest(algorithm, content, resources=None, meter=None):
    if algorithm not in SUPPORTED_RECORD_ALGORITHMS:
        return None
    from .bounds import reserve,release_live
    tick=reserve(resources,meter,live_bytes=8192,work_bytes=8192)
    if tick: raise DigestStop(tick)
    state=chunks=piece=digest=None
    try:
        import base64
        import hashlib
        state = hashlib.new(algorithm)
        chunks=_chunks(content, resources, meter)
        for piece in chunks:
            state.update(piece)
        tick=reserve(resources,meter,live_bytes=1024,work_bytes=1024)
        if tick: raise DigestStop(tick)
        digest = state.digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    finally:
        piece=None
        try:
            if chunks is not None: chunks.close()
        finally:
            chunks=None
            digest=None
            state=None
            release_live(meter,8192)


def crc32(data, resources=None, meter=None):
    try:
        selected = meter.get("codec_modules", {}).get("deflate") if isinstance(meter, dict) else None
        if isinstance(selected, dict):
            zlib = selected["module"]
        else:
            import zlib
    except ImportError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return None, "runtime_zlib_module_absent"
    except MemoryError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return None, "resource_ceiling_exceeded"
    state = 0
    chunks=_chunks(data, resources, meter)
    try:
        for piece in chunks:
            state = zlib.crc32(piece, state)
    except DigestStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,stop)
        return None, stop.cause
    finally:
        piece=None
        chunks.close()
    return state & 0xFFFFFFFF, None
