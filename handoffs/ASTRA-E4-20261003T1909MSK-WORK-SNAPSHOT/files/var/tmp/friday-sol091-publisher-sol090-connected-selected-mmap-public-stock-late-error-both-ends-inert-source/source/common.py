"""Bounded wire/canonical primitives. No authority is inferred from a label."""
import errno
import hashlib
import json
import os
import stat
import selectors
import struct
import time
import copy

INPUT_MAX = 2_000_000
DOCUMENT_MAX = 80_000_000
OUTPUT_MAX = 33_554_432
READ_MAX = 40_960_000_000
RAM_MAX = 8_589_934_592
SLOTS_MAX = 128
MEMBERS_MAX = 512
ARTIFACTS_MAX = 350
WORKERS_MAX = 4
WALL_MAX = 4200
REFUSAL_RESERVE = 4096
KINDS = frozenset(("member", "node-archive", "node-shasums256",
    "ubuntu-archive", "ubuntu-inrelease", "ubuntu-packages", "wheel",
    "kernel", "native", "data", "browser", "candidate", "golden",
    "unrar", "custody"))


class Refused(Exception):
    def __init__(self, cause, phase="before-effect", detail=None):
        self.cause, self.phase, self.detail = cause, phase, detail
        super().__init__(cause)


def exact(value, fields, cause="schema"):
    if type(value) is not dict or len(value) > 512 or set(value) != set(fields):
        raise Refused(cause)
    return value


def integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Refused("integer")
    return value


def text(value, maximum=512, minimum=1):
    if type(value) is not str or not minimum <= len(value) <= maximum or "\0" in value:
        raise Refused("text")
    return value


def digest(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise Refused("digest")
    return value


def sha(raw, meter=None, admitted=None):
    """Admit a complete memory-hash pass in the owning meter before hashing.

    A same-object owned digest is reused only for the identical immutable
    bytes object. Kernel rchar is not a substitute. No meter and no admitted
    hold is the pre-owner residue; every in-domain caller passes one.
    """
    if type(raw) is not bytes and type(raw) is not bytearray:
        raise Refused("hash_preimage")
    size = len(raw)
    if size > READ_MAX:
        raise Refused("hash_preimage")
    if meter is not None and type(raw) is bytes:
        cached = meter.cached_digest(raw)
        if cached is not None:
            return cached
    local = None
    if admitted is not None:
        admitted.commit(hash_bytes=size)
    elif meter is not None:
        local = meter.reserve("memory-hash", hash_bytes=size, allocation=size + 64)
        local.commit(hash_bytes=size)
    try:
        digest = hashlib.sha256(raw).hexdigest()
    finally:
        if local is not None:
            local.release()
    if meter is not None and type(raw) is bytes:
        meter.remember_digest(raw, digest)
    return digest


class PreObserverHash:
    """Same-pid hash credit before RootObserver exists. Not a new owner."""
    def __init__(self):
        self.pid = os.getpid()
        self.hash_bytes = 0
        self.read_bytes=0;self.token=0;self.slots=3;self.closed=False
        from lifetime import FDState
        from capacity import connected_export_authority
        self.allocation=connected_export_authority()['canonical_allocation']+INPUT_MAX
        self.fd_state=FDState(self.token)
        self.journals=[];self.observer=None
    def retain_journal(self,journal):
        if journal not in self.journals:self.journals.append(journal)
    def before_read(self,maximum):
        integer(maximum,READ_MAX)
        if self.observer is not None:
            self.observer.before_physical_sample(maximum)
            return
        if self.read_bytes+self.hash_bytes+maximum>READ_MAX:raise Refused("aggregate_read")
    def read_debit(self,size):
        self.before_read(size);self.read_bytes+=size
        if self.observer is not None:self.observer.explicit_read+=size
    def release(self):
        if self.closed:return True
        if getattr(self,'fd_rows',{}) or any(j.fds for j in self.journals):return False
        if self.observer is not None and self.observer.release(self.token) is not True:return False
        # The descriptor domain retains complete histories until the actual
        # final completion receipt; dropping the sampling list is not enough.
        self.closed=True
        return True
    def commit(self, reads=0, output=0, hash_bytes=0):
        if os.getpid() != self.pid:
            raise Refused("observer_owner")
        size = integer(hash_bytes, READ_MAX)
        if self.observer is not None:self.observer.before_physical_sample(0,additional_hash=size)
        if self.hash_bytes+self.read_bytes > READ_MAX - size:
            raise Refused("aggregate_read")
        self.hash_bytes += size
        if self.observer is not None:self.observer.explicit_hash+=size


class OwnedPreimage:
    """Full strong immutable bytes/digest, computed on an actual prepaid hold."""
    def __init__(self,raw,meter,hold):
        if type(raw) is not bytes:raise Refused("immutable_preimage")
        self.raw=raw
        self.digest=sha(raw,meter,hold)
    def for_same_object(self,raw):
        if raw is not self.raw or type(raw) is not bytes:raise Refused("immutable_preimage_identity")
        return self.digest


def mono():
    return time.monotonic_ns()


def error_fact(exc, phase, delivered=0):
    """Full bounded OS error facts; text is retained separately as raw UTF-8."""
    return {"class": type(exc).__name__, "cause": getattr(exc, "cause", "exception"),
        "errno": getattr(exc, "errno", None), "strerror": getattr(exc, "strerror", None),
        "filename": getattr(exc, "filename", None), "filename2": getattr(exc, "filename2", None),
        "phase": phase, "delivered_bytes": delivered,
        "text": str(exc), "effects_possible": delivered != 0}


def _encoded_string_size(value):
    size = 2
    for ch in value:
        c = ord(ch)
        size += (2 if ch in '"\\\b\f\n\r\t' else 6 if c < 32 or 127 <= c <= 65535
                 else 12 if c > 65535 else 1)
    return size


def encoded_bound(value, maximum=INPUT_MAX, max_depth=24, max_string=INPUT_MAX):
    """Calculate the exact ASCII encoding size before allocating its wire."""
    count, active = 0, set()
    def walk(item, depth):
        nonlocal count
        count += 1
        if count > maximum or depth > max_depth:
            raise Refused("canonical_capacity")
        if type(item) is dict:
            if len(item) > 512 or id(item) in active:
                raise Refused("canonical_capacity")
            active.add(id(item))
            size = 2 + max(0, len(item)-1)
            for k, v in item.items():
                if type(k) is not str or len(k) > max_string:
                    raise Refused("canonical_key")
                size += _encoded_string_size(k) + 1 + walk(v, depth+1)
                if size+1 > maximum: raise Refused("canonical_capacity")
            active.remove(id(item))
            return size
        if type(item) is list:
            if len(item) > 512 or id(item) in active:
                raise Refused("canonical_capacity")
            active.add(id(item))
            size = 2 + max(0, len(item)-1)
            for child in item:
                size += walk(child, depth+1)
                if size+1 > maximum: raise Refused("canonical_capacity")
            active.remove(id(item))
            return size
        if type(item) is str:
            if len(item) > max_string: raise Refused("canonical_string")
            return _encoded_string_size(item)
        if type(item) is bool: return 4 if item else 5
        if item is None: return 4
        if type(item) is int:
            if not -(10**21) <= item <= 10**21: raise Refused("canonical_integer")
            return len(str(item))
        raise Refused("canonical_type")
    size = walk(value, 1)+1
    if size > maximum: raise Refused("canonical_capacity")
    return size


# lossless-history-codec-start
def _fail(cause):
    raise Refused(cause)


def _history_chunks(items):
    if not items:
        return []
    chunks=[items[i:i + 512] for i in range(0,len(items),512)]
    if len(chunks)>512:_fail("history_chunks")
    return chunks


_HISTORY_ABSENT = -2
_HISTORY_NONE = -1
_HISTORY_FIELDS = (
    "holder", "credit", "status", "identity9_decimal_strings", "attempted_close",
    "close_history", "acquisition_error", "last_known_holder", "last_known_credit",
    "history_arena", "close_policy", "cancellation_acknowledged", "parent_pid",
    "parent_acquisition",
)
_HISTORY_ROW_KEYS = ("slot", "generation", "fd")
_HISTORY_PATTERN_LEN = len(_HISTORY_FIELDS) + 1


def _history_cell_key(value):
    if value is None:
        return ("N",)
    kind = type(value)
    if kind is bool:
        return ("B", value)
    if kind is int:
        return ("I", value)
    if kind is str:
        return ("S", value)
    return ("J", json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def encode_history_rows(rows):
    """Share identical identity9 tuples and repeated closed-row columns.

    Slot, generation and fd stay on every row. Absent keys stay absent.
    None stays None and is not the status string UNKNOWN.
    """
    if type(rows) is not list:
        _fail("history_rows")
    cells = []
    cell_at = {}
    patterns = []
    pattern_at = {}
    triples = []

    def intern(value):
        key = _history_cell_key(value)
        found = cell_at.get(key)
        if found is None:
            found = len(cells)
            cell_at[key] = found
            cells.append(value)
        return found

    for row in rows:
        if type(row) is not dict:
            _fail("history_row")
        vector = []
        for name in _HISTORY_FIELDS:
            if name not in row:
                vector.append(_HISTORY_ABSENT)
            elif row[name] is None:
                vector.append(_HISTORY_NONE)
            else:
                vector.append(intern(row[name]))
        extra = {key: value for key, value in row.items() if key not in _HISTORY_FIELDS and key not in _HISTORY_ROW_KEYS}
        vector.append(_HISTORY_ABSENT if not extra else intern(extra))
        key = tuple(vector)
        index = pattern_at.get(key)
        if index is None:
            index = len(patterns)
            pattern_at[key] = index
            patterns.append(vector)

        def cell_or_sentinel(name):
            if name not in row:
                return _HISTORY_ABSENT
            if row[name] is None:
                return _HISTORY_NONE
            if type(row[name]) is not int:
                _fail("history_row_index")
            return row[name]

        triples.append([index, cell_or_sentinel("slot"), cell_or_sentinel("generation"), cell_or_sentinel("fd")])
    codec = {"schema": "friday.a190.lossless-history-rows.v1", "cells": _history_chunks(cells),
        "patterns": _history_chunks(patterns), "rows": _history_chunks(triples)}
    if len(codec["cells"]) > 512 or len(codec["patterns"]) > 512 or len(codec["rows"]) > 512:
        _fail("history_chunks")
    return codec


def _history_flat(chunks, cause):
    if type(chunks) is not list or len(chunks) > 512:
        _fail(cause)
    out = []
    for part in chunks:
        if type(part) is not list or len(part) > 512:
            _fail(cause)
        out.extend(part)
    return out


def decode_history_rows(codec):
    """Rebuild every row in order from the shared tables. Values are copied."""
    if type(codec) is not dict or codec.get("schema") != "friday.a190.lossless-history-rows.v1":
        _fail("history_codec")
    if set(codec) != {"schema", "cells", "patterns", "rows"}:
        _fail("history_codec")
    cells = _history_flat(codec.get("cells"), "history_cells")
    patterns = _history_flat(codec.get("patterns"), "history_patterns")
    triples = _history_flat(codec.get("rows"), "history_rows")
    restored = []
    for triple in triples:
        if type(triple) is not list or len(triple) != 4 or any(type(item) is not int for item in triple):
            _fail("history_triple")
        index = triple[0]
        if not 0 <= index < len(patterns):
            _fail("history_pattern")
        pattern = patterns[index]
        if type(pattern) is not list or len(pattern) != _HISTORY_PATTERN_LEN or any(type(item) is not int for item in pattern):
            _fail("history_pattern")
        row = {}
        for name, ptr in zip(_HISTORY_FIELDS, pattern):
            if ptr == _HISTORY_ABSENT:
                continue
            if ptr == _HISTORY_NONE:
                row[name] = None
                continue
            if not 0 <= ptr < len(cells):
                _fail("history_cell")
            row[name] = copy.deepcopy(cells[ptr])
        extra_ptr = pattern[-1]
        if extra_ptr != _HISTORY_ABSENT:
            if not 0 <= extra_ptr < len(cells) or type(cells[extra_ptr]) is not dict:
                _fail("history_extra")
            row.update(copy.deepcopy(cells[extra_ptr]))
        identity = row.get("identity9_decimal_strings", None)
        if "identity9_decimal_strings" in row and identity is not None:
            if type(identity) is not list or len(identity) != 9 or any(type(part) is not str for part in identity):
                _fail("history_identity9")
        if "close_history" in row and row["close_history"] is not None and type(row["close_history"]) is not list:
            _fail("history_close")
        if "cancellation_acknowledged" in row and row["cancellation_acknowledged"] is not None and type(row["cancellation_acknowledged"]) is not bool:
            _fail("history_flag")
        for name, sentinel in (("slot", triple[1]), ("generation", triple[2]), ("fd", triple[3])):
            if sentinel == _HISTORY_ABSENT:
                continue
            row[name] = None if sentinel == _HISTORY_NONE else sentinel
        restored.append(row)
    return restored


# The existing codec is the only full chronology. Collection descriptors
# select it in order; they never carry another members/order chronology.
def history_collection(count, identity, generation, domain_identity, owner_pid):
    return {"collection_identity": str(identity), "kind": "list",
        "owner_pid": owner_pid, "generation": generation,
        "domain_identity": domain_identity, "journal_row_list": True,
        "whole_domain": False, "row_count": count,
        "projection": "history_codec.rows"}

def validate_history_collection(value, count=None):
    fields={"collection_identity","kind","owner_pid","generation",
        "domain_identity","journal_row_list","whole_domain","row_count","projection"}
    if type(value) is not dict or set(value)!=fields:
        _fail("history_collection")
    if (type(value["collection_identity"]) is not str or value["kind"]!="list"
            or type(value["owner_pid"]) is not int
            or value["journal_row_list"] is not True
            or value["whole_domain"] is not False
            or value["projection"]!="history_codec.rows"
            or type(value["row_count"]) is not int or not 0<=value["row_count"]<=65536):
        _fail("history_collection")
    for name, kind in (("generation",int),("domain_identity",str)):
        if value[name] is not None and type(value[name]) is not kind:
            _fail("history_collection")
    if count is not None and value["row_count"]!=count:
        _fail("history_collection_count")
    return value

def history_counted_chunks(chunks, count, cause):
    if type(count) is not int or not 0<=count<=512*512:
        _fail(cause)
    rows=_history_flat(chunks,cause)
    if len(rows)!=count:
        _fail(cause)
    return rows

def history_book_rows(book):
    if type(book) is not dict or book.get("truncated") is not False:
        _fail("history_book")
    rows=decode_history_rows(book.get("history_codec"))
    if (type(book.get("journal_count")) is not int or len(rows)>65536
            or len(rows)!=book["journal_count"]):
        _fail("history_book_count")
    validate_history_collection(book.get("collection"),len(rows))
    credits=history_counted_chunks(book.get("credit_chunks"),book.get("credit_count"),"history_credits")
    faults=history_counted_chunks(book.get("fault_chunks"),book.get("fault_count"),"history_faults")
    tokens={}
    for credit in credits:
        if (type(credit) is not dict or set(credit)!={"token","slots","closed"}
                or type(credit["slots"]) is not int or not 0<=credit["slots"]<=128
                or type(credit["closed"]) is not bool
                or type(credit["token"]) not in (str,int) or credit["token"] in tokens):
            _fail("history_credit")
        tokens[credit["token"]]=credit
    active=[]
    for row in rows:
        if type(row.get("credit")) not in (str,int) or row["credit"] not in tokens:
            _fail("history_row_credit")
        if row.get("status") in ("ACQUIRED","HELD","UNKNOWN","ROOT_PREOWNED_BIND_PENDING","PREOWNED_CHILD_TABLE"):
            if type(row.get("fd")) is not int:
                _fail("history_row_fd")
            active.append(row["fd"])
    if type(book.get("pending_fds")) is not list or book["pending_fds"]!=sorted(active):
        _fail("history_pending")
    return rows

def history_domain_books(domain):
    if (type(domain) is not dict or set(domain)!={"domain_identity","arena_token","generation",
            "history_count","history_max","journal_count","journal_chunks","truncated"}
            or type(domain["domain_identity"]) is not str or domain["history_max"]!=65536
            or domain["truncated"] is not False):
        _fail("history_domain")
    if type(domain["generation"]) is not int or not 0<=domain["generation"]<=10**21:
        _fail("history_domain_generation")
    if type(domain["history_count"]) is not int or not 0<=domain["history_count"]<=65536:
        _fail("history_domain_count")
    books=history_counted_chunks(domain["journal_chunks"],domain["journal_count"],"history_journals")
    identities=set();row_identities=set();total=0
    for book in books:
        rows=history_book_rows(book);total+=len(rows)
        collection=book["collection"]
        identity=collection["collection_identity"]
        if (collection["domain_identity"]!=domain["domain_identity"]
                or collection["generation"]!=domain["generation"] or identity in identities):
            _fail("history_domain_alias")
        identities.add(identity)
        for row in rows:
            key=tuple((name in row,row.get(name)) for name in ("credit","slot","generation"))
            if key in row_identities:_fail("history_domain_row_duplicate")
            row_identities.add(key)
    if total!=domain["history_count"]:_fail("history_domain_history_count")
    return books

def fork_history_book(packet):
    if type(packet) is not dict or packet.get("schema") not in ("friday.a181.fork-owner-graph.v2","friday.sol090.fork-owner-full.v3"):
        _fail("fork_history_packet")
    reference=packet.get("inherited")
    if type(reference) is not dict or set(reference)!={"collection_identity"}:
        _fail("fork_history_reference")
    books=history_domain_books(packet.get("new_journals"))
    matched=[book for book in books if book["collection"]["collection_identity"]==reference["collection_identity"]]
    if len(matched)!=1:
        _fail("fork_history_reference")
    book=matched[0]
    if (book.get("owner_pid")!=packet.get("owner_pid")
            or book.get("parent_pid")!=packet.get("parent_pid")
            or book["collection"]["owner_pid"]!=packet.get("owner_pid")):
        _fail("fork_history_owner")
    return book,books


def _history_wire(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def lossless_history_upper():
    """Selected prefix-closed installer chronology plus one fork census.

    A 240-byte member name has at most 120 components. Directory parents are
    selected members, so repeated closes share one identity9 tuple. The wire
    is this codec once, inside the FD-domain journal, not 37 times a cap.
    """
    depth = 240 // 2
    directories = depth - 1
    members = 512
    files = members - directories
    close_ok = [{"attempt": 1, "error": None, "status": "CLOSED"}]
    close_bad = [{"attempt": 1, "error": {"class": "OSError", "errno": 9, "text": "EBADF"}, "status": "UNKNOWN"}]
    idents = []
    for number in range(members + 1 + 129):
        base = 10 ** 19 + number
        idents.append([str(base + field) for field in range(9)])
    holders = (
        "installer-parent", "installer-output", "installer-root", "installer-seal",
        "pipe", "child-inherited", "sealed-source-bundle", "actor-child-stdout",
        "actor-child-stderr", "actor-pidfd", "actor-exe", "scope-pidfd",
        "native-pidfd", "child-stdout", "child-stderr", "selector-inotify",
        "open-beneath-parent", "open-beneath-result", "open-absolute-root",
        "prepared-output", "output-file", "root-tool-exe", "prepared-path",
        "bootstrap-inherited", "delivery-a", "delivery-b", "delivery-c",
        "delivery-d", "delivery-e", "delivery-f", "delivery-g", "root",
    )
    rows = []
    fork_rows = []
    slot = 0

    def add(bucket, identity, holder, credit, status, close_history, policy, **extra):
        nonlocal slot
        slot += 1
        row = {"fd": slot % 128, "holder": holder, "credit": credit, "status": status,
            "identity9_decimal_strings": identity, "slot": slot, "generation": slot,
            "attempted_close": None if status == "PREOWNED" else status, "close_history": close_history,
            "acquisition_error": None if status != "NO_RETURN" else {"class": "OSError", "errno": 9, "phase": "terminal", "text": "census"},
            "last_known_holder": holder, "last_known_credit": credit, "history_arena": 0,
            "close_policy": policy, "cancellation_acknowledged": status in ("NO_RETURN", "CLOSED")}
        row.update(extra)
        bucket.append(row)

    parent_credit = "installer-fd-hold"
    for index in range(directories):
        for _ in range(directories - index):
            add(rows, idents[index], "installer-parent", parent_credit, "CLOSED", close_ok, "EXEC_CLOEXEC")
    for index in range(files):
        for _ in range(depth):
            add(rows, idents[index % directories], "installer-parent", parent_credit, "CLOSED", close_ok, "EXEC_CLOEXEC")
        add(rows, idents[directories + index], "installer-output", "file-credit-%04d" % index, "CLOSED", close_ok, "EXEC_CLOEXEC")
    for index in range(directories):
        add(rows, idents[index], "installer-seal", parent_credit, "CLOSED", close_ok, "EXEC_CLOEXEC")
    add(rows, idents[members], "installer-root", parent_credit, "CLOSED", close_ok, "EXEC_CLOEXEC")
    for index in range(129):
        add(fork_rows, idents[members + 1 + index], "child-inherited", "fork-census", "HELD", [], "KEEP",
            parent_pid=1000, parent_acquisition={"fd": index, "slot": index, "status": "HELD",
                "identity9_decimal_strings": idents[members + 1 + index]})
    for index in range(16):
        add(rows, idents[index], holders[index % len(holders)], "pipe-credit", "CLOSED", close_ok, "EXEC_CLOEXEC")
    for index in range(directories):
        add(rows, idents[index], "installer-parent", parent_credit, "UNKNOWN", close_bad, None)
        add(rows, idents[index], "installer-parent", parent_credit, "NO_RETURN", close_bad, None)
    for holder in holders:
        add(rows, idents[0], holder, parent_credit, "CLOSED", close_ok, "EXEC_CLOEXEC")
    probe = dict(rows[0])
    probe["generation"] = 2 ** 53 + 3
    probe.pop("parent_pid", None)
    parsed = json.loads(_history_wire(encode_history_rows([probe, rows[-1]])))
    got = decode_history_rows(parsed)
    if got[0]["generation"] != 2 ** 53 + 3 or got[0]["identity9_decimal_strings"] != probe["identity9_decimal_strings"]:
        _fail("history_round_trip")
    if "parent_pid" in got[0] or got[0]["acquisition_error"] is not None or got[1]["holder"] != rows[-1]["holder"]:
        _fail("history_round_trip")
    codec = encode_history_rows(rows)
    fork = encode_history_rows(fork_rows)
    journal = {"credits": [{"closed": False, "slots": 4, "token": i} for i in range(512)], "faults": [],
        "history_arena": 0, "history_codec": codec, "journal_count": len(rows),
        "pending_fds": list(range(128)), "truncated": False}
    domain = {"arena_token": 0, "generation": len(rows) + len(fork_rows), "history_count": len(rows) + len(fork_rows),
        "history_max": 65536, "journal_count": 2, "journal_chunks": [[journal, {
            "credits": [{"closed": False, "slots": 129, "token": "fork"}], "faults": [], "history_codec": fork,
            "journal_count": len(fork_rows), "owner_pid": 2, "parent_pid": 1,
            "pending_fds": list(range(129)), "truncated": False}]], "truncated": False}
    raw = _history_wire(domain)
    return {"schema": "friday.a190.lossless-history-rows.v1", "reachable_history_rows": len(rows) + len(fork_rows),
        "pattern_count": sum(len(part) for part in codec["patterns"]) + sum(len(part) for part in fork["patterns"]),
        "encoded_upper": len(raw), "fits_single_wire": len(raw) <= INPUT_MAX, "directories": directories,
        "files": files, "fork_census_rows": len(fork_rows), "pipe_rows": 16, "holder_literals": len(holders)}
# lossless-history-codec-end

def canonical(value, meter=None, maximum=INPUT_MAX, depth=24):
    n = encoded_bound(value, maximum, depth)
    hold = None if meter is None else meter.reserve("encode", allocation=n*4+1024, reads=n, output=0)
    try:
        raw = (json.dumps(value, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False)+"\n").encode("ascii")
        if len(raw) != n: raise Refused("canonical_size")
        if hold is not None: hold.commit(reads=n)
        if hold is not None:
            meter.own_result(raw,hold);hold=None
        return raw
    finally:
        if hold is not None: hold.release()


def domain(name, value, meter=None, maximum=INPUT_MAX):
    text(name, 128)
    if not name.isascii() or any(c.isspace() for c in name): raise Refused("domain")
    body = canonical(value, meter, maximum)
    try:
        # sha() admits the separate memory-hash pass. It is not a physical read.
        return sha(name.encode("ascii") + b"\0" + body, meter)
    finally:
        if meter is not None: meter.retire_result(body)


def json_preflight(raw, maximum=INPUT_MAX, max_depth=24):
    """Lexical capacity scan precedes json.loads, including string/token bounds."""
    if type(raw) is not bytes or not 1 <= len(raw) <= maximum or not raw.endswith(b"\n"):
        raise Refused("json_size")
    stack, in_string, escaped, string_bytes, nodes = [], False, False, 0, 0
    strings=containers=scalars=colons=commas=0;scalar_open=False
    for c in raw:
        if in_string:
            string_bytes += 1
            if string_bytes > maximum: raise Refused("json_string")
            if escaped: escaped = False
            elif c == 92: escaped = True
            elif c == 34: in_string = False
            elif c < 32: raise Refused("json_string")
            continue
        if c == 34:
            in_string, string_bytes = True, 0
            strings+=1;scalar_open=False
        elif c in (91, 123):
            containers+=1;scalar_open=False
            stack.append([c, 0])
            if len(stack) > max_depth: raise Refused("json_depth")
        elif c in (93, 125):
            scalar_open=False
            if not stack or stack[-1][0] != (91 if c == 93 else 123): raise Refused("json_shape")
            stack.pop()
        elif c == 44 and stack:
            commas+=1;scalar_open=False
            stack[-1][1] += 1
            if stack[-1][1] >= 512: raise Refused("json_items")
        elif c==58:colons+=1;scalar_open=False
        elif c in (9,10,13,32):scalar_open=False
        elif not scalar_open:scalars+=1;scalar_open=True
        nodes += 1
        if nodes > maximum: raise Refused("json_capacity")
    if in_string or escaped or stack: raise Refused("json_shape")
    # Lexically counted BEFORE loads: UTF decode/string overlap, containers,
    # scalar objects, pair-hook tuples/dict entries and list growth. This is a
    # conservative qualified 64-bit CPython bound, not a post-allocation RSS
    # estimate nor a fixed 256*body charge repeated for mostly long strings.
    return len(raw)*8+(strings+containers+scalars)*256+colons*256+commas*64+65536


def parse(raw, meter=None, maximum=INPUT_MAX, depth=24):
    allocation=json_preflight(raw, maximum, depth)
    hold = None if meter is None else meter.reserve("parse", allocation=allocation, reads=len(raw))
    def pairs(rows):
        out = {}
        for k, v in rows:
            if k in out: raise Refused("duplicate_key")
            out[k] = v
        return out
    def nonfinite(_):
        raise Refused("nonfinite")
    try:
        try:
            value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs,
                               parse_constant=nonfinite)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise Refused("json") from exc
        checked=canonical(value,meter,maximum,depth)
        try:
            if checked!=raw:raise Refused("noncanonical")
        finally:
            if meter is not None:meter.retire_result(checked)
        if hold is not None: hold.commit(reads=len(raw))
        if hold is not None:
            meter.own_result(value,hold);hold=None
        return value
    finally:
        if hold is not None: hold.release()


def bounded_tree_paths(root, limit=512, directory_mode=None, files_only=False, refuse_symlinks=False):
    """Refuse before the path past limit is stored. Do not follow links."""
    if type(root) is not str or type(limit) is not int or not 0 <= limit <= MEMBERS_MAX:
        raise Refused("complete_image_inventory")
    if not root.startswith("/") or "//" in root or any(part in ("", ".", "..") for part in root[1:].split("/")):
        raise Refused("physical_path")
    found = []
    stack = [root]
    seen = 0
    while stack:
        directory = stack.pop()
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refused("source_directory")
        if directory_mode is not None and stat.S_IMODE(info.st_mode) != directory_mode:
            raise Refused("source_directory")
        with os.scandir(directory) as scanned:
            pending = []
            for entry in scanned:
                seen += 1
                if seen > limit:
                    raise Refused("complete_image_inventory")
                path = entry.path
                if entry.is_symlink():
                    if refuse_symlinks:
                        raise Refused("source_symlink")
                    found.append(path)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    if files_only:
                        continue
                found.append(path)
            if len(stack) + len(pending) > limit:
                raise Refused("complete_image_inventory")
            stack.extend(pending)
    return found


def bounded_refusal(cause, phase, delivered, cleanup_ok):
    """Preallocated-form refusal: no JSON walk, exhausted-meter entry or user text."""
    allowed = {"before-effect", "preparation", "after-delivery", "execution", "postdelivery", "terminal"}
    p = phase if phase in allowed else "terminal"
    # Cause is a separately retained error preimage; public wire is fixed capacity.
    return (b'{"GO":false,"cause":"root_provider_refused","cleanup_ok":'
        + (b"true" if cleanup_ok else b"false")
        + b',"delivered_bytes":'+str(min(max(delivered,0),OUTPUT_MAX)).encode("ascii")
        + b',"effects_granted":false,"phase":"'+p.encode("ascii")
        + b'","runtime":"NOT_RUN","status":"REFUSED"}\n')


class Frame:
    """Root-created pipes, finite full-frame IO, exact EOF and partial delivery facts."""
    def __init__(self, incoming, outgoing, deadline, meter, maximum=INPUT_MAX, journal=None, owner_hold=None):
        self.incoming, self.outgoing = incoming, outgoing
        self.deadline, self.meter, self.maximum = deadline, meter, maximum
        self.delivered = 0
        self.owner_hold=owner_hold;self.last_prepaid=False
        if journal is None or incoming not in journal.fds or outgoing not in journal.fds:
            raise Refused("frame_existing_owner")
        self.journal=journal
        os.set_blocking(incoming, False); os.set_blocking(outgoing, False)

    def _ready(self, fd, mask):
        left = (self.deadline-mono())/1e9
        if left <= 0: raise Refused("deadline", "after-delivery" if self.delivered else "before-effect")
        with selectors.PollSelector() as s:
            s.register(fd, mask)
            if not s.select(left): raise Refused("deadline", "after-delivery")

    def send_raw(self, raw):
        if type(raw) is not bytes or len(raw) > self.maximum: raise Refused("frame_size")
        hold = self.meter.reserve("pipe-delivery", output=len(raw)+8, allocation=8)
        try:
            for blob in (struct.pack(">Q", len(raw)), raw):
                at = 0
                while at < len(blob):
                    self._ready(self.outgoing, selectors.EVENT_WRITE)
                    try: n = os.write(self.outgoing, memoryview(blob)[at:])
                    except BlockingIOError: continue
                    if n <= 0: raise Refused("pipe_write", "after-delivery")
                    at += n; self.delivered += n; hold.commit(output=n)
        finally:
            hold.release()

    def send(self, value):
        raw=canonical(value,self.meter,self.maximum)
        try:self.send_raw(raw)
        finally:self.meter.retire_result(raw)

    def send_prepaid(self,value):
        if self.owner_hold is None or self.owner_hold.closed:raise Refused("owner_transport_not_preadmitted","terminal")
        raw=canonical(value,maximum=self.maximum)
        for blob in (struct.pack(">Q",len(raw)|(1<<63)),raw):
            at=0
            while at<len(blob):
                self._ready(self.outgoing,selectors.EVENT_WRITE)
                try:n=os.write(self.outgoing,memoryview(blob)[at:])
                except BlockingIOError:continue
                if n<=0:raise Refused("owner_transport_delivery","terminal")
                at+=n;self.delivered+=n;self.owner_hold.commit(output=n)

    def _read_prepaid(self,size):
        if self.owner_hold is None or self.owner_hold.closed or size>self.maximum:raise Refused("owner_transport_not_preadmitted","terminal")
        out=bytearray(size);at=0
        while at<size:
            self._ready(self.incoming,selectors.EVENT_READ)
            try:part=os.read(self.incoming,min(65536,size-at))
            except BlockingIOError:continue
            if not part:raise Refused("owner_transport_eof","terminal")
            out[at:at+len(part)]=part;at+=len(part);self.owner_hold.commit(reads=len(part))
        return bytes(out)

    def _read(self, size):
        hold = self.meter.reserve("pipe-receive", reads=size, allocation=size*2+65536)
        try:
            out = bytearray(size); at = 0
            while at < size:
                self._ready(self.incoming, selectors.EVENT_READ)
                try: part = os.read(self.incoming, min(65536, size-at))
                except BlockingIOError: continue
                if not part: raise Refused("premature_eof", "after-delivery")
                out[at:at+len(part)] = part; at += len(part); hold.commit(reads=len(part))
            body=bytes(out)
            self.meter.own_result(body,hold);hold=None
            return body
        finally:
            if hold is not None:hold.release()

    def receive_raw(self,force_prepaid=False):
        # The fixed header is prepaid before knowing the message kind; this
        # cannot re-enter ordinary admission after the execution deadline.
        header=self._read_prepaid(8) if self.owner_hold is not None else self._read(8)
        try:n=struct.unpack(">Q",header)[0]
        finally:
            if self.owner_hold is None:self.meter.retire_result(header)
        self.last_prepaid=force_prepaid or bool(n&(1<<63));n=n&((1<<63)-1)
        if n > self.maximum: raise Refused("frame_size", "after-delivery")
        return self._read_prepaid(n) if self.last_prepaid else self._read(n)

    def receive(self,force_prepaid=False):
        raw=self.receive_raw(force_prepaid)
        try:return parse(raw,None if self.last_prepaid else self.meter,self.maximum)
        finally:
            if not self.last_prepaid:self.meter.retire_result(raw)

    def close(self):
        faults = []
        for attribute in ("incoming","outgoing"):
            fd=getattr(self,attribute)
            if fd<0:continue
            self.journal.close_one(fd)
            if fd not in self.journal.fds:setattr(self,attribute,-1)
        faults.extend(self.journal.faults)
        return faults
