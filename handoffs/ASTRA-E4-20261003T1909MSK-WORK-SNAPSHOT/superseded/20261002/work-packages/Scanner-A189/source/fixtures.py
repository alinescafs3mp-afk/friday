"""Ordinary stored-wheel builder only. No historical unsafe generators exist here."""

import struct
from .digests import crc32

_NAMES = ("demo-1.0.dist-info/METADATA", "demo-1.0.dist-info/WHEEL", "demo-1.0.dist-info/RECORD")


def build_zip(files, method=0):
    if method not in (0, 8) or not isinstance(files, tuple) or tuple(name for name, data in files) != _NAMES:
        raise ValueError("ordinary_fixture_shape")
    if any(not isinstance(data, bytes) or len(data) > 4096 for name, data in files):
        raise ValueError("ordinary_fixture_shape")
    local_parts = []
    central_parts = []
    offset = 0
    for name, data in files:
        name_b = name.encode("ascii")
        if method == 8:
            import zlib
            compressor = zlib.compressobj(wbits=-15)
            stored = compressor.compress(data) + compressor.flush()
        else:
            stored = data
        pair = crc32(data)
        if not isinstance(pair, tuple) or len(pair) != 2 or pair[1] is not None or not isinstance(pair[0], int) or isinstance(pair[0], bool):
            raise ValueError("fixture_crc")
        digest = pair[0]
        local = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 2048, method, 0, 0, digest, len(stored), len(data), len(name_b), 0) + name_b + stored
        central = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | 20, 20, 2048, method, 0, 0,
                              digest, len(stored), len(data), len(name_b), 0, 0, 0, 0, 0o100644 << 16, offset) + name_b
        local_parts.append(local)
        central_parts.append(central)
        offset += len(local)
    local = b"".join(local_parts)
    central = b"".join(central_parts)
    return local + central + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(files), len(files), len(central), len(local), 0)
