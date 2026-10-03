"""Canonical JSON used by broker projections. Duplicate keys and non-finite numbers are rejected."""

import json


def canonical_bytes(obj):
    payload = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return payload.encode("ascii") + b"\n"


class _BoundExceeded(Exception):
    pass


def canonical_resource_bound(obj):
    """Exact ASCII length and conservative cost of the existing encoder.

    This metadata preflight constructs no JSON strings or byte buffers. It is
    not a CPython/native allocation proof or a producer pre-allocation lease.
    Shared aliases occur once per encoded position; cycles cannot be JSON.
    """
    from .bounds import HARD_MAX_OUTPUT_BYTES, HARD_MAX_DEPTH
    active=set()
    size=1
    work=4096
    overhead=4096
    def string(value):
        nonlocal size,work,overhead
        size+=2
        work+=((len(value)+2047)//2048)*49152
        for char in value:
            code=ord(char)
            size+=2 if char in ('"','\\','\b','\f','\n','\r','\t') else 1 if 32<=code<=126 else 6 if code<=65535 else 12
        overhead+=256
    def walk(value,depth):
        nonlocal size,work,overhead
        if depth>HARD_MAX_DEPTH: raise ValueError('canonical_depth')
        if value is None: size+=4
        elif value is True: size+=4
        elif value is False: size+=5
        elif isinstance(value,int) and not isinstance(value,bool):
            if value.bit_length()>128: raise ValueError('canonical_integer')
            number=abs(value); digits=1
            while number>=10: number//=10; digits+=1
            size+=digits+(1 if value<0 else 0); work+=128
        elif isinstance(value,str): string(value)
        elif isinstance(value,(list,dict)):
            ident=id(value)
            if ident in active: raise ValueError('canonical_cycle')
            active.add(ident)
            try:
                size+=2+max(0,len(value)-1)
                overhead+=len(value)*128+512
                if isinstance(value,dict):
                    work+=len(value)*64
                    for key,item in value.items():
                        if not isinstance(key,str): raise ValueError('canonical_key')
                        string(key); size+=1; walk(item,depth+1)
                else:
                    for item in value: walk(item,depth+1)
            finally: active.remove(ident)
        else: raise ValueError('canonical_type')
        if size>HARD_MAX_OUTPUT_BYTES: raise ValueError('canonical_size')
    try: walk(obj,1)
    except (ValueError,RecursionError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None
    return {'output_bytes':size,'work_bytes':work+size*4,
            'live_bytes':size*4+overhead+98304}


def canonical_bytes_bounded(obj, limit, resources=None, meter=None):
    """Encode canonical JSON, refusing to grow the buffer past limit."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return None
    out = None
    outstanding = 0
    success = False
    returned_live = 0

    def allocate(amount, work=0):
        nonlocal outstanding
        if isinstance(meter, dict):
            from .bounds import reserve
            if reserve(resources, meter, work_bytes=work, live_bytes=amount):
                raise _BoundExceeded()
            outstanding += amount

    def free(amount):
        nonlocal outstanding
        if isinstance(meter, dict):
            from .bounds import release_live
            release_live(meter, amount)
            outstanding -= amount

    def emit(chunk):
        if len(out) + len(chunk) > limit:
            raise _BoundExceeded()
        allocate(len(chunk)*2+64,len(chunk))
        out.extend(chunk)

    def string(value):
        emit(b'"')
        for offset in range(0, len(value), 2048):
            # A bounded scalar chunk permits a fixed pre-allocation reservation.
            allocate(98304, 49152)
            try:
                escaped = json.dumps(value[offset:offset + 2048], ensure_ascii=True, allow_nan=False)[1:-1].encode("ascii")
                emit(escaped)
            finally:
                escaped=None
                free(98304)
        emit(b'"')

    def walk(value):
        if value is None:
            emit(b"null")
        elif value is True:
            emit(b"true")
        elif value is False:
            emit(b"false")
        elif isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > 128:
                raise _BoundExceeded()
            # The decimal string and ASCII copy exist before emit can reserve
            # the destination. Own that scalar scratch before constructing it.
            allocate(256, 128)
            scalar = None
            try:
                scalar = str(value).encode("ascii")
                emit(scalar)
            finally:
                scalar = None
                free(256)
        elif isinstance(value, str):
            string(value)
        elif isinstance(value, list):
            emit(b"[")
            for index, item in enumerate(value):
                if index:
                    emit(b",")
                walk(item)
            emit(b"]")
        elif isinstance(value, dict):
            mapping_reservation = len(value) * 64
            allocate(mapping_reservation, mapping_reservation)
            try:
                emit(b"{")
                for index, key in enumerate(sorted(value)):
                    if not isinstance(key, str):
                        raise ValueError("unsupported")
                    if index:
                        emit(b",")
                    string(key)
                    emit(b":")
                    walk(value[key])
                emit(b"}")
            finally:
                free(mapping_reservation)
        else:
            raise ValueError("unsupported")

    try:
        # Own the empty buffer and bounded local encoder state before creation.
        allocate(4096, 4096)
        out = bytearray()
        walk(obj)
        emit(b"\n")
        allocate(len(out))
        result = bytes(out)
        returned_live = len(result)
        success = True
        return result
    except _BoundExceeded as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None
    finally:
        # On success the one returned bytes allocation transfers to the caller.
        # All exceptional exits, including host allocation/type failures, retire
        # every admitted local mapping/string/output reservation symmetrically.
        # The bytearray must be dead before its allocation domain is retired.
        # Only the distinct returned bytes reservation leaves this scope.
        out = None
        free(max(0, outstanding - (returned_live if success else 0)))


def canonical_sha256(obj):
    import hashlib

    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def canonical_sha256_metered(obj, resources, meter):
    """Bound and sample whole projection/material hashing before its buffers grow."""
    ceilings = resources.get("ceilings") if isinstance(resources, dict) else None
    cap = ceilings.get("max_output_bytes", 67108864) if isinstance(ceilings, dict) else 67108864
    raw = canonical_bytes_bounded(obj, cap, resources, meter)
    if raw is None:
        return None, "resource_ceiling_exceeded"
    from .digests import content_sha256, DigestStop
    try:
        digest = content_sha256(raw, resources, meter)
        return digest, None
    except DigestStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,stop)
        return None, stop.cause
    finally:
        from .bounds import release_live
        amount = len(raw)
        raw = None
        release_live(meter, amount)


def _walk(obj, depth, max_depth):
    if depth > max_depth:
        raise ValueError("document_too_deep")
    if isinstance(obj, dict):
        for value in obj.values():
            _walk(value, depth + 1, max_depth)
    elif isinstance(obj, list):
        for value in obj:
            _walk(value, depth + 1, max_depth)
    elif isinstance(obj, str):
        if "\x00" in obj:
            raise ValueError("nul_in_string")
    elif obj is None or isinstance(obj, (bool, int)):
        return
    elif isinstance(obj, float):
        raise ValueError("non_integer_number")
    else:
        raise ValueError("unsupported_json_type")


def canonical_loads(data, max_bytes=1048576, max_depth=32):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("document_not_bytes")
    if len(data) > max_bytes:
        raise ValueError("document_too_large")
    if not data.endswith(b"\n"):
        raise ValueError("missing_trailing_lf")

    def pairs(items):
        keys = [key for key, _value in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_key")
        return dict(items)

    def reject_constant(name):
        raise ValueError("non_finite_number")

    obj = json.loads(
        data[:-1].decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=reject_constant,
    )
    _walk(obj, 1, max_depth)
    if canonical_bytes(obj) != bytes(data):
        raise ValueError("not_canonical")
    return obj
