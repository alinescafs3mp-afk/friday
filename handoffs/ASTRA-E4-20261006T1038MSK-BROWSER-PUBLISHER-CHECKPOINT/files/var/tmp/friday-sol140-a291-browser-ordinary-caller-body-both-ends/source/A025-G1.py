#!/usr/bin/python3
"""Exact A023 download-only executor. Default invocation has no write/network effects.

Root must independently review this source before --execute-reviewed-a023 SHA256.
Downloaded bytes are only streamed/hashed or parsed as bounded inert metadata.
No downloaded imports, extraction, installation, shell, retries or redirects.
"""
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import re
import resource
import select
import signal
import ssl
import stat
import struct
import sys
import time
import urllib.parse

# Actual objects remain private to this loaded owner, not reconstructed from
# legacy failure strings. Full graph transport is conditional on existing caps.
ERROR_CUSTODY=[]
def remember_error(exc,stage):
    entry={'error':exc,'stage':stage,'pid':os.getpid(),'parent':os.getppid(),
        'traceback':exc.__traceback__,'at_ns':str(time.monotonic_ns()),
        'preformat_snapshot':None,'preformat_snapshot_complete':False}
    ERROR_CUSTODY.append(entry)
    try:
        entry['preformat_snapshot']=causal_error_DATA([exc])
        entry['preformat_snapshot_complete']=True
    except BaseException as secondary:
        # No recursive recorder and no formatting of either real origin.
        entry['snapshot_constructor_error']=secondary
        ERROR_CUSTODY.append({'error':secondary,'stage':'preformat_snapshot_constructor',
            'pid':os.getpid(),'parent':os.getppid(),'traceback':secondary.__traceback__,
            'at_ns':str(time.monotonic_ns()),'preformat_snapshot':None,
            'preformat_snapshot_complete':False})
    return exc

BASE = "/home/jericho/.jericho/runtime/subagent-lifecycle/"
PLAN = BASE + "ASTRA-E4-MATERIAL-ACQUISITION-PLAN-A023-G1-RESULT.json"
PLAN_SHA = "8cb910917a816e39adf63355433d7a6ceb06979c066923c68fbbfbcd19ff4110"
OWNER = "/home/jericho/.jericho/grok-takeover/ASTRA-E4-OWNER-DELEGATED-PROJECT-AUTHORITY-20261001.json"
OWNER_SHA = "a360c1f8e7b6e09b95bfea0ea13af257234a815733a796046f5b9f0167fd7a63"
ROOT = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
CA = "/etc/ssl/certs/ca-certificates.crt"
CA_SHA = "80eedd808e4cbd6fd42e125da2ea225fd1365d8e29edef8bcc45ff8bc7044ce2"
CHUNK = 1048576
HEADERS = 65536
MSK = datetime.timezone(datetime.timedelta(hours=3))
LIMITS = {"network_gets_max": 210, "exact_size_digest_archive_gets": 202,
          "exact_size_digest_archive_bytes": 372570142, "publisher_metadata_gets": 5,
          "url_known_unpinned_browser_gets": 3, "transfer_body_bytes_max": 1329461278,
          "per_request_header_bytes_max": HEADERS, "workers_global_max": 4,
          "retries": 0, "connect_timeout_sec": 15,
          "small_request_total_timeout_sec": 120, "large_request_total_timeout_sec": 300,
          "wall_clock_sec": 1800, "quarantine_bytes_max": 2147483648,
          "regular_files_max": 320, "directories_max": 13,
          "rss_bytes_max": 268435456, "stream_chunk_bytes_max": CHUNK}
STOP = False


class Refused(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def now():
    return datetime.datetime.now(MSK).isoformat(timespec="seconds")


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def inert_json(data, maximum=8388608):
    require(len(data) <= maximum, "JSON_SIZE")
    # Bound depth before invoking recursive JSON decoding, ignoring quoted text.
    depth = 0
    quoted = escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            require(depth <= 32, "JSON_DEPTH")
        elif byte in (93, 125):
            depth -= 1
            require(depth >= 0, "JSON_STRUCTURE")
    require(depth == 0 and not quoted, "JSON_STRUCTURE")
    try:
        result = json.loads(data, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refused("JSON_CONSTANT")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused("JSON_PARSE") from exc
    pending = [result]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        require(nodes <= 100000, "JSON_NODES")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            require(len(item) <= maximum, "JSON_STRING")
    return result


def relpath(path):
    require(isinstance(path, str) and 0 < len(path) <= 512, "PATH_LENGTH")
    parts = path.split("/")
    require(all(p not in ("", ".", "..") and re.fullmatch(r"[A-Za-z0-9_.+~%-]+", p)
                for p in parts), "PATH_ESCAPE")
    return parts


def absolute_open(path):
    require(path.startswith("/") and "\x00" not in path, "INPUT_ABSOLUTE")
    parts = path.split("/")[1:]
    require(all(p not in ("", ".", "..") for p in parts), "INPUT_PATH")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        return fd
    finally:
        os.close(directory)


def file_stat(fd, output=False):
    st = os.fstat(fd)
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "FILE_TYPE_NLINK")
    require(st.st_uid in (0, os.getuid()) and not st.st_mode & 0o7022, "INPUT_OWNER_MODE")
    if output:
        require(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600,
                "OUTPUT_OWNER_MODE")
    else:
        require(not st.st_mode & 0o111, "INPUT_EXECUTABLE")
    return st


def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_uid, st.st_gid, st.st_mode, st.st_nlink)


def digest_fd(fd, cap, expected=None, size=None, collect=False):
    before = file_stat(fd)
    require(before.st_size <= cap and (size is None or before.st_size == size), "INPUT_SIZE")
    os.lseek(fd, 0, os.SEEK_SET)
    sha = hashlib.sha256()
    count = 0
    chunks = []
    while True:
        data = os.read(fd, min(CHUNK, cap - count + 1))
        if not data:
            break
        count += len(data)
        require(count <= cap, "INPUT_GROWTH")
        sha.update(data)
        if collect:
            chunks.append(data)
    require(identity(before) == identity(file_stat(fd)), "INPUT_DRIFT")
    value = sha.hexdigest()
    require(expected is None or value == expected, "INPUT_SHA")
    require(size is None or count == size, "INPUT_SIZE")
    os.lseek(fd, 0, os.SEEK_SET)
    return count, value, b"".join(chunks) if collect else None


def pinned_json(path, sha):
    fd = absolute_open(path)
    try:
        _, _, data = digest_fd(fd, 8388608, expected=sha, collect=True)
        return inert_json(data), data
    finally:
        os.close(fd)


def compile_plan(plan):
    bill = plan["acquisition_bill"]
    require(plan["schema"] == "friday.astra.e4.material-acquisition-plan.v1" and
            plan["assignment"] == "ASTRA-E4-MATERIAL-ACQUISITION-PLAN-A023" and
            plan["generation"] == 1, "PLAN_IDENTITY")
    require(bill["quarantine"] == ROOT and bill["limits"] == LIMITS and
            all(type(v) is int for v in bill["limits"].values()), "BILL_LIMITS")
    require(plan["authority"]["owner_authority_pin"] == {"path": OWNER, "sha256": OWNER_SHA},
            "OWNER_PIN")
    require(bill["transport"] == {"method": "GET", "scheme": "https",
            "certificate_and_hostname_validation": "REQUIRED_NO_BYPASS",
            "ambient_proxy_credentials_cookies_and_netrc": False, "redirects": False,
            "exact_urls_only": True, "http_status_required": 200, "accept_encoding": "identity",
            "unexpected_content_encoding": "REFUSE",
            "unknown_wire_bytes": "Report as unobserved, never zero; accounting caps response-body bytes only."},
            "TRANSPORT_BILL")
    require([len(bill[k]) for k in ("ubuntu106", "kernel_verification_archives",
            "python94_wheels", "remote_metadata", "browser_archives", "local_input_copies")]
            == [106, 2, 94, 5, 3, 100], "BILL_COUNTS")
    remote = []
    for kind, rows in (("ubuntu", bill["ubuntu106"] + bill["kernel_verification_archives"]),
                       ("python", bill["python94_wheels"]),
                       ("metadata", bill["remote_metadata"]),
                       ("browser", bill["browser_archives"])):
        for row in rows:
            pin = row["archive"] if kind == "ubuntu" else row
            size, sha = pin.get("size"), pin.get("sha256")
            cap = size if size is not None else row["bytes_max"]
            require(type(cap) is int and 0 < cap <= 536870912, "BODY_CAP")
            require(sha is None or re.fullmatch(r"[0-9a-f]{64}", sha), "BODY_SHA")
            require((kind in ("ubuntu", "python")) == (size is not None and sha is not None),
                    "PIN_PRESENCE")
            url = urllib.parse.urlsplit(row["url"])
            host = {"ubuntu": {"archive.ubuntu.com"}, "python": {"files.pythonhosted.org"},
                    "metadata": {"nodejs.org", "raw.githubusercontent.com", "googlechromelabs.github.io"},
                    "browser": {"storage.googleapis.com", "cdn.playwright.dev"}}[kind]
            require(url.scheme == "https" and url.hostname in host and url.port is None and
                    url.username is None and url.password is None and not url.query and
                    not url.fragment and url.path.startswith("/") and
                    all(32 < ord(c) < 127 for c in row["url"]), "URL_AUTHORITY")
            relpath(row["relative_path"])
            if kind == "ubuntu":
                require(row["url"] == "https://archive.ubuntu.com/ubuntu/" + pin["filename"],
                        "UBUNTU_ROUTE")
            remote.append({"url": row["url"], "relative_path": row["relative_path"],
                           "size": size, "sha256": sha, "cap": cap, "kind": kind,
                           "host": url.hostname, "path": url.path,
                           "seconds": 300 if cap > 16777216 else 120})
    local = [dict(r, source=r["source"], size=r.get("bytes"),
                  cap=r.get("bytes", r.get("bytes_max"))) for r in bill["local_input_copies"]]
    provenance = bill["local_provenance_copies"]
    for row in provenance["selected_packages"] + provenance["selected_inrelease"] + [provenance["keyring"]]:
        local.append(dict(row, source=row["path"], cap=row["size"]))
    node = bill["node"]
    require(node["network_archive_get_scheduled"] is False, "NODE_DUPLICATE_GET")
    local.append(dict(node, source=node["local_path"], cap=node["size"]))
    for row in local:
        relpath(row["relative_path"])
        require(row["source"].startswith("/") and type(row["cap"]) is int and
                0 < row["cap"] <= 33554432 and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                "LOCAL_SCHEMA")
    paths = [r["relative_path"] for r in remote + local] + ["bill.json", "transport-receipts.ndjson", "inventory.json"]
    dirs = bill["retained_inventory"]["dirs"]
    require(len(paths) == len(set(paths)) == 320 and set(paths) == set(bill["retained_inventory"]["paths"]),
            "INVENTORY_PATH_COLLISION_OR_DRIFT")
    require(len(dirs) == len(set(dirs)) == 12 and
            set(dirs) == {"/".join(relpath(p)[:i]) for p in paths for i in range(1, len(relpath(p)))},
            "INVENTORY_DIRS")
    require(sum(r["cap"] for r in remote) == LIMITS["transfer_body_bytes_max"] and
            sum(r["size"] for r in remote if r["size"] is not None) == 372570142,
            "BODY_SUM")
    return remote, local, paths, dirs


class Output:
    """Exclusive descriptor-relative owned tree; never overwrite, unlink or reuse."""
    def __init__(self, path, paths, dirs):
        require(os.getuid() != 0, "ROOT_UID_REFUSED")
        parent, name = os.path.split(path)
        self.parent = absolute_open(parent)
        require(stat.S_ISDIR(os.fstat(self.parent).st_mode), "QUARANTINE_PARENT_TYPE")
        os.mkdir(name, 0o700, dir_fd=self.parent)
        self.name = name
        self.root = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent)
        self.fds = {"": self.root}
        self.identities = {}
        self.paths = set(paths)
        self.created = set()
        self.file_ids = {}
        for rel in dirs:
            parts = relpath(rel)
            base = self.fds["/".join(parts[:-1])]
            os.mkdir(parts[-1], 0o700, dir_fd=base)
            self.fds[rel] = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
        for key, fd in self.fds.items():
            st = os.fstat(fd)
            require(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700, "DIRECTORY_MODE")
            self.identities[key] = (st.st_dev, st.st_ino)

    def check(self):
        root = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
        require((root.st_dev, root.st_ino) == self.identities[""], "ROOT_CUSTODY")
        for key, fd in self.fds.items():
            st = os.fstat(fd)
            require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and
                    stat.S_IMODE(st.st_mode) == 0o700 and (st.st_dev, st.st_ino) == self.identities[key],
                    "DIRECTORY_CUSTODY")
            if key:
                parts = relpath(key)
                linked = os.stat(parts[-1], dir_fd=self.fds["/".join(parts[:-1])], follow_symlinks=False)
                require((linked.st_dev, linked.st_ino) == self.identities[key], "DIRECTORY_REPLACED")
            expected = {p.split("/")[-1] for p in self.fds if p and "/".join(p.split("/")[:-1]) == key}
            expected.update(p.split("/")[-1] for p in self.created if "/".join(p.split("/")[:-1]) == key)
            require(set(os.listdir(fd)) == expected, "UNEXPECTED_TREE_ENTRY")
        for path, expected in self.file_ids.items():
            parts = relpath(path)
            st = os.stat(parts[-1], dir_fd=self.fds["/".join(parts[:-1])], follow_symlinks=False)
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid() and
                    stat.S_IMODE(st.st_mode) == 0o600 and (st.st_dev, st.st_ino) == expected,
                    "FILE_CUSTODY")

    def create(self, path):
        self.check()
        require(path in self.paths and path not in self.created, "OUTPUT_COLLISION")
        parts = relpath(path)
        fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fds["/".join(parts[:-1])])
        st = file_stat(fd, True)
        self.created.add(path)
        self.file_ids[path] = (st.st_dev, st.st_ino)
        return fd

    def read(self, path, cap):
        self.check()
        parts = relpath(path)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=self.fds["/".join(parts[:-1])])
        try:
            file_stat(fd, True)
            return digest_fd(fd, cap, collect=True)[2]
        finally:
            os.close(fd)

    def close(self):
        first = None
        owned = list(self.fds.values()) + [self.parent]
        self.fds.clear()
        self.parent = None
        for fd in owned:
            if fd is None: continue
            try: os.close(fd)
            except BaseException as exc:
                if first is None: first = exc
                remember_error(exc, 'G1.Output.close')
        if first is not None: raise first


def write_all(fd, data):
    while data:
        n = os.write(fd, data)
        require(n > 0, "SHORT_WRITE")
        data = data[n:]


def copy_input(row, fd, out, checkpoint):
    require(identity(file_stat(fd)) == checkpoint, "INPUT_CHANGED_AFTER_PREFLIGHT")
    target = out.create(row["relative_path"])
    try:
        sha = hashlib.sha256()
        count = 0
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            require(not STOP, "OWNER_STOP")
            data = os.read(fd, min(CHUNK, row["cap"] - count + 1))
            if not data:
                break
            count += len(data)
            require(count <= row["cap"], "COPY_CAP")
            write_all(target, data)
            sha.update(data)
        require(identity(file_stat(fd)) == checkpoint and sha.hexdigest() == row["sha256"] and
                (row["size"] is None or count == row["size"]), "COPY_INTEGRITY")
        os.fsync(target)
        file_stat(target, True)
        out.check()
        return {"state": "LOCAL_HASH_MATCH", "bytes": count, "sha256": sha.hexdigest()}
    finally:
        os.close(target)


class HeaderReader:
    def __init__(self, reader):
        self.reader = reader
        self.count = 0

    def readline(self, limit=-1):
        line = self.reader.readline(min(HEADERS + 1, limit) if limit >= 0 else HEADERS + 1)
        self.count += len(line)
        require(self.count <= HEADERS, "HEADERS_OR_CHUNK_FRAMING_CAP")
        return line

    def __getattr__(self, name):
        return getattr(self.reader, name)


class BoundedResponse(http.client.HTTPResponse):
    def begin(self):
        self.fp = HeaderReader(self.fp)
        super().begin()


def validate_headers(response, entry):
    headers = response.getheaders()
    lengths = [v for k, v in headers if k.lower() == "content-length"]
    encoding = [v.lower().strip() for k, v in headers if k.lower() == "content-encoding"]
    transfer = [v.lower().strip() for k, v in headers if k.lower() == "transfer-encoding"]
    require(response.status == 200, "HTTP_STATUS_" + str(response.status))
    require(not encoding or encoding == ["identity"], "CONTENT_ENCODING")
    require(len(lengths) <= 1 and (not transfer or transfer == ["chunked"]) and
            not (lengths and transfer), "HTTP_FRAMING")
    length = None
    if lengths:
        require(re.fullmatch(r"[0-9]{1,12}", lengths[0]) is not None, "CONTENT_LENGTH")
        length = int(lengths[0])
        require(length <= entry["cap"] and (entry["size"] is None or length == entry["size"]),
                "DECLARED_BODY_SIZE")
    return headers, length


ERROR_EMISSIONS=[]

def emit(fd, event):
    # The complete frame is retained before the first write.
    # write_returned, EOF, digest, and process exit are not receiver acceptance.
    entry={'event':event,'body':None,'written':0,'write_returned':False,
        'receiver_accepted':False,'fd':fd,'preowned':None,'exit_is_handover':False,
        'eof_is_handover':False,'digest_is_handover':False,'pending_is_handover':False}
    ERROR_EMISSIONS.append(entry)
    try:
        payload=sol069_encode(event,a201_encode_cap(131068),require)
        physical=sol069_preowned_backing(payload)
        wire=physical['physical_reference'] if physical['physical_reference'] is not None else payload
        data=len(wire).to_bytes(4,'big')+wire
        require(len(data)<=131072,'WORKER_RECEIPT_CAP')
        require(sol069_worker_literal_exceeds(WORKER_LITERAL_BODY_MIN_FAILURE),'WORKER_LITERAL_DOMAIN_RED')
        require(sol069_literal_frame_exceeds(CAP_PROBE_BYTES,0,PACKET_CAP),'LITERAL_CAP_PROBE_CANNOT_FIT')
        require(CONSTANT_RUN_STORED==5,'CONSTANT_RUN_STORED')
        entry['preowned']=physical
        entry['physical_original_frame']=payload
        entry['body']=data
        require(bytes(entry['preowned']['view'])==entry['physical_original_frame'] and
                entry['preowned']['bound_before_channel_write'] and
                entry['preowned']['receiver_accepted'] is False,'PREOWNED_BEFORE_WRITE')
        while entry['written']<len(entry['body']):
            count=os.write(fd,memoryview(entry['body'])[entry['written']:])
            require(type(count) is int and count>0,'SOL069_EVENT_NO_WRITE_PROGRESS')
            entry['written']+=count
        entry['write_returned']=True
        entry['receiver_accepted']=False
        entry['preowned']['receiver_accepted']=False
    except BaseException as exc:
        entry['error']=exc
        remember_error(exc,'G1.emit.constructor_or_partial_write_before_owner_loss')
        raise


def causal_error_DATA(errors):
    """Lossless bounded DATA for the actual ordinary first/secondary error graph.
    Original objects stay in their owner. Unknown argument types are not called
    or stringified and cannot receive complete-error transport credit.
    """
    nodes, identities, values, value_ids = [], {}, [], {}
    def scalar(value):
        if value is None or type(value) in (str, int, bool):
            return {"kind": "scalar", "value": value}
        if type(value) is float:
            return {"kind":"float64","network_hex":struct.pack("!d",value).hex()}
        if type(value) in (bytes,bytearray):
            key=id(value)
            if key in value_ids:return {"kind":"value_ref","index":value_ids[key]}
            index=len(values);value_ids[key]=index
            values.append({"index":index,"kind":type(value).__name__,"body":bytes(value)})
            return {"kind":"value_ref","index":index}
        if isinstance(value,BaseException):
            return {"kind":"exception","index":visit(value)}
        if type(value) in (tuple,list,dict,set,frozenset):
            key=id(value)
            if key in value_ids:return {"kind":"value_ref","index":value_ids[key]}
            require(len(values)<4096,"ERROR_ARGUMENT_GRAPH_TRANSPORT_BOUND")
            index=len(values);value_ids[key]=index
            row={"index":index,"kind":type(value).__name__,"items":[]};values.append(row)
            row["items"]=([{"key":scalar(k),"value":scalar(v)} for k,v in value.items()]
                if type(value) is dict else [scalar(v) for v in value])
            return {"kind":"value_ref","index":index}
        raise Refused("ERROR_ARGUMENT_TRANSPORT_UNSUPPORTED")
    def visit(error):
        if error is None:
            return None
        if not isinstance(error, BaseException):
            error = Refused(error)
        key = id(error)
        if key in identities:
            return identities[key]
        require(len(nodes) < 4096, "ERROR_GRAPH_TRANSPORT_BOUND")
        index = len(nodes); identities[key] = index
        row = {"index": index, "module": type(error).__module__,
               "type": type(error).__qualname__, "args": None,
               "errno": getattr(error, "errno", None),
               "filename": getattr(error, "filename", None),
               "filename2": getattr(error, "filename2", None),
               "suppress_context": error.__suppress_context__,
               "cause": None, "context": None, "members": []}
        nodes.append(row)
        row["args"]=scalar(error.args)
        row['attributes']=scalar(error.__dict__)
        row['notes']=scalar(getattr(error,'__notes__',[]))
        stock={}
        for name in ('name','path','obj','encoding','object','start','end','reason','verify_code',
                'verify_message','library','msg','lineno','offset','text','end_lineno','end_offset','print_file_and_line'):
            if hasattr(error,name):stock[name]=scalar(getattr(error,name))
        row['stock_attributes']=stock
        row['traceback']=[];trace=error.__traceback__
        while trace is not None:
            require(len(row['traceback'])<4096,'ERROR_TRACEBACK_TRANSPORT_BOUND')
            code=trace.tb_frame.f_code
            row['traceback'].append({'filename':code.co_filename,'name':code.co_name,
                'qualname':code.co_qualname,'line':trace.tb_lineno,'lasti':trace.tb_lasti})
            trace=trace.tb_next
        row['frame_locals_private_not_transported']=True
        row["cause"] = visit(error.__cause__)
        row["context"] = visit(error.__context__)
        if isinstance(error, BaseExceptionGroup):
            row["members"] = [visit(v) for v in error.exceptions]
        return index
    roots = [visit(error) for error in errors]
    return {"schema": "friday.sol064.ordinary-error-graph.v1", "roots": roots,
            "first": roots[0] if roots else None, "nodes": nodes,
            "value_nodes":values,"argument_codec":"typed-full-binary-identity-graph.sol069.v1",
            "complete": True, "original_objects_retained_in_owner": True}


def worker(entry, body_fd, event_fd, context):
    """Blocking DNS/TLS/body I/O confined to one killable owned process."""
    result = {"event": "FINAL", "state": "FAIL", "body_complete": False,
              "wire_bytes": None, "headers": None, "tls": None}
    connection = None
    count = 0
    sha = hashlib.sha256()
    try:
        os.environ.clear()
        resource.setrlimit(resource.RLIMIT_AS, (48 * 1048576, 48 * 1048576))
        resource.setrlimit(resource.RLIMIT_CPU, (330, 330))
        resource.setrlimit(resource.RLIMIT_FSIZE, (entry["cap"], entry["cap"]))
        connection = http.client.HTTPSConnection(entry["host"], port=443, timeout=15,
                                                  context=context)
        connection.response_class = BoundedResponse
        connection.connect()
        peer = connection.sock.getpeercert(binary_form=True)
        protocol = connection.sock.version()
        require(peer and protocol in ("TLSv1.2", "TLSv1.3") and context.check_hostname and
                context.verify_mode == ssl.CERT_REQUIRED, "TLS_EVIDENCE")
        result["tls"] = {"hostname": entry["host"], "certificate_and_hostname_verified": True,
                         "verify_mode": "CERT_REQUIRED", "protocol": protocol,
                         "peer_certificate_sha256": hashlib.sha256(peer).hexdigest()}
        emit(event_fd, {"event": "CONNECTED", "tls": result["tls"]})
        # HTTPSConnection does not consult proxy/auth/netrc/cookie environment.
        connection.request("GET", entry["path"], headers={"Accept-Encoding": "identity",
                           "Connection": "close", "User-Agent": "Friday-A023-inert-acquisition/1"})
        response = connection.getresponse()
        result["http_status"] = response.status
        result["headers"] = response.getheaders()
        _, declared = validate_headers(response, entry)
        while count < entry["cap"]:
            data = response.read(min(CHUNK, entry["cap"] - count))
            if not data:
                break
            require(count + len(data) <= entry["cap"], "BODY_CAP")
            write_all(body_fd, data)
            sha.update(data)
            count += len(data)
        require(count < entry["cap"] or response.isclosed(), "CAP_REACHED_WITHOUT_EOF")
        require(declared is None or count == declared, "BODY_TRUNCATED")
        require(entry["size"] is None or count == entry["size"], "BODY_SIZE")
        require(entry["sha256"] is None or sha.hexdigest() == entry["sha256"], "BODY_SHA")
        result["state"] = "HASH_MATCH" if entry["sha256"] else "UNACCEPTED_UNPINNED_BODY"
        result["body_complete"] = True
        os.fsync(body_fd)
        file_stat(body_fd, True)
    except BaseException as exc:
        remember_error(exc,'G1.worker.before_legacy_failure')
        try:result["failure"] = str(exc)[:512] if isinstance(exc, Refused) else type(exc).__name__
        except BaseException as secondary:
            remember_error(secondary,'G1.worker.legacy_format_secondary')
            result['failure']='FORMAT_FAILED_NO_BODY_CREDIT'
    finally:
        result.update(bytes=count, sha256=sha.hexdigest())
        if connection is not None:
            try:connection.close()
            except BaseException as exc:
                remember_error(exc,'G1.worker.connection_close')
                result['body_complete']=False
                result.setdefault('failure',type(exc).__name__)
        try:os.close(body_fd)
        except BaseException as exc:
            remember_error(exc,'G1.worker.body_close')
            result['body_complete']=False
            result.setdefault('failure',type(exc).__name__)
        result['original_error_graph']=None
        # Graph construction is not end/partial-write/close retirement proof.
        result['complete_original_error_transport']=False
        result['captured_error_graph_complete']=False
        result['preformat_error_snapshots']=[{'stage':v['stage'],'at_ns':v['at_ns'],
            'graph':v['preformat_snapshot'],'complete':v['preformat_snapshot_complete']}
            for v in ERROR_CUSTODY]
        try:
            result['original_error_graph']=causal_error_DATA([v['error'] for v in ERROR_CUSTODY])
            result['captured_error_graph_complete']=True
        except BaseException as exc:
            remember_error(exc,'G1.worker.graph_constructor')
            result['body_complete']=False
        try:
            emit(event_fd, result)
        except BaseException as exc:
            remember_error(exc,'G1.worker.final_encoder_or_write')
            raise
        finally:
            try:os.close(event_fd)
            except BaseException as exc:
                remember_error(exc,'G1.worker.event_close_after_emission')
                raise


def process_status(pid):
    with open("/proc/" + str(pid) + "/status", encoding="ascii") as stream:
        text = stream.read(65536)
    match = re.search(r"^VmRSS:\s+(\d+) kB$", text, re.M)
    return int(match[1]) * 1024 if match else 0  # exited/zombie has no resident pages


def stop_owned(pid, pidfd, hard=None):
    """Signal exact owned pidfd only; finite wait, never kill groups/foreign PIDs."""
    try:
        if pidfd is None:
            # Only the already-recorded direct, never-reaped launch is eligible.
            os.kill(pid, signal.SIGKILL)
        else:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        pass
    end = min(time.monotonic() + 1.0, hard) if hard is not None else time.monotonic() + 1.0
    while time.monotonic() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return True, status
        select.select([], [], [], 0.02)
    return False, None


def sol076_launch_intent(child):
    """Ordinary Python launch intention, not a native birth/retirement grant."""
    child['sol076'] = {'schema': 'friday.sol076.ordinary-launch.v1',
        'fds': [], 'original_errors': [], 'first_error': None, 'cleanup_fault': False}
    child['fork_attempted'] = False
    child['fork_is_stock'] = False
    return child


def sol076_launch_error(child, exc, stage):
    journal = child['sol076']
    # Keep the original object before any diagnostic formatting or cleanup.
    journal['original_errors'].append((stage, exc))
    if journal['first_error'] is None:
        journal['first_error'] = (stage, exc)
    remember_error(exc, 'SOL076.' + stage)
    if child.get('pid') is None:
        child['lifecycle'] = ('BIRTH_UNCONFIRMED' if child['fork_attempted'] and
                              not child['fork_is_stock'] else 'NOT_CREATED')


def sol076_own_fd(child, key, fd):
    child[key] = fd
    child['sol076']['fds'].append({'slot': key, 'fd': fd,
        'close_attempted': False, 'close_confirmed': False, 'close_error': None})
    return fd


def sol076_close_slot(child, key, run=None, closer=None):
    journal = child.get('sol076')
    fd = child.get(key)
    if journal is None:
        if fd is not None:
            if closer is None: os.close(fd)
            else: closer(fd, run)
            child[key] = None
        return
    rows = [row for row in journal['fds'] if row['slot'] == key]
    if not rows: return
    row = rows[-1]
    if row['close_attempted']: return
    row['close_attempted'] = True
    try:
        if closer is None:
            os.close(row['fd'])
            confirmed = True
        else:
            first_error_index = len(run.original_errors) if run is not None else 0
            confirmed = closer(row['fd'], run) is True
            if run is not None and not confirmed:
                for exc in run.original_errors[first_error_index:]:
                    journal['original_errors'].append(('close_' + key, exc))
                    if journal['first_error'] is None:
                        journal['first_error'] = ('close_' + key, exc)
        row['close_confirmed'] = confirmed
        if not confirmed:
            journal['cleanup_fault'] = True
            row['close_error'] = 'CLOSE_OR_OBSERVATION_UNCONFIRMED'
    except BaseException as exc:
        journal['cleanup_fault'] = True
        row['close_error'] = type(exc).__name__
        sol076_launch_error(child, exc, 'close_' + key)
        if run is not None:
            run.original_errors.append(exc)
            run.fail('CLEANUP:' + type(exc).__name__, True)
    finally:
        # A close syscall is attempted once. An uncertain fd is never retried or
        # treated as a newly acquired/recycled descriptor by later cleanup.
        child[key] = None


def sol076_launch_DATA(child):
    journal = child['sol076']
    born = child.get('pid') is not None
    first = journal['first_error']
    return {'schema': journal['schema'], 'relative_path': child['entry']['relative_path'],
        'birth': 'CREATED' if born else ('UNCONFIRMED' if child['lifecycle'] == 'BIRTH_UNCONFIRMED'
                                      else 'NOT_CREATED' if first else 'PENDING'),
        'pid': child.get('pid'), 'lifecycle': child['lifecycle'],
        'first_failure': None if first is None else {'stage': first[0], 'type': type(first[1]).__name__},
        'errors': [{'stage': stage, 'type': type(exc).__name__} for stage, exc in journal['original_errors']],
        'descriptor_records': [dict(row) for row in journal['fds']],
        'cleanup_fault': journal['cleanup_fault'],
        'ordinary_cleanup_complete': not journal['cleanup_fault'] and all(
            row['close_attempted'] and row['close_confirmed'] for row in journal['fds']),
        'stop_confirmed': born and child['lifecycle'] == 'REAPED',
        'child_retirement_accepted': False, 'native_body_retirement_accepted': False,
        'original_error_objects_retained_locally': True}


def sol076_finish_record(child, record, run=None, closer=None):
    if 'sol076' not in child: return record
    for key in ('write_pipe', 'pipe', 'pidfd', 'body'):
        sol076_close_slot(child, key, run, closer)
    launch = sol076_launch_DATA(child)
    record['launch'] = launch
    if (launch['birth'] != 'CREATED' or not launch['ordinary_cleanup_complete'] or
            child['lifecycle'] != 'REAPED'):
        record.setdefault('worker_failure_before_cleanup', record.get('failure'))
        record.update(body_complete=False, terminal_completion=False,
            acceptance_complete=False, stop_confirmed=launch['stop_confirmed'],
            accounting_charged_bytes=child['entry']['cap'],
            failure='LAUNCH_NOT_CREATED' if launch['birth'] == 'NOT_CREATED' else
                    'LAUNCH_BIRTH_UNCONFIRMED' if launch['birth'] == 'UNCONFIRMED' else 'LAUNCH_CLEANUP_UNCONFIRMED')
    return record


def sol076_publish_receipt(receipt_fd, record, item):
    """A provisional line is not a qualified terminal. The qualified line is written only after fsync and readback. A later fault appends a finite end line."""
    provisional = dict(record)
    provisional['receipt_commitment'] = 'PROVISIONAL'
    provisional['receipt_qualified'] = False
    blob = json.dumps(provisional, separators=(',', ':')).encode() + b'\n'
    digest = hashlib.sha256(blob).hexdigest()

    def mark_fault():
        item['sol076']['cleanup_fault'] = True
        record['receipt_commitment'] = 'PUBLICATION_FAULT'
        record['receipt_qualified'] = False
        record['body_complete'] = False
        record['terminal_completion'] = False
        record['acceptance_complete'] = False
        record['provisional_sha256'] = digest
        record['launch'] = sol076_launch_DATA(item)

    def append_end(cause):
        try:
            size = os.lseek(receipt_fd, 0, os.SEEK_END)
            if size and os.pread(receipt_fd, 1, size - 1) != b'\n':
                write_all(receipt_fd, b'\n')
            inv = {'schema': 'friday.sol076.receipt-end.v1', 'receipt_commitment': 'END_UNCONFIRMED',
                   'receipt_qualified': False, 'provisional_sha256': digest, 'finite_end': cause,
                   'relative_path': record.get('relative_path')}
            write_all(receipt_fd, json.dumps(inv, separators=(',', ':')).encode() + b'\n')
            os.fsync(receipt_fd)
        except BaseException as end_exc:
            sol076_launch_error(item, end_exc, 'receipt_end')

    def durable(payload):
        offset = os.lseek(receipt_fd, 0, os.SEEK_CUR)
        write_all(receipt_fd, payload)
        held = os.pread(receipt_fd, len(payload), offset)
        require(held == payload, 'RECEIPT_RECEIVER_READBACK')
        os.fsync(receipt_fd)
        held = os.pread(receipt_fd, len(payload), offset)
        require(held == payload, 'RECEIPT_FSYNC_READBACK')

    try:
        durable(blob)
    except BaseException as exc:
        sol076_launch_error(item, exc, 'provisional_receipt')
        mark_fault()
        append_end('RECEIPT_PROVISIONAL_UNCONFIRMED')
        raise
    qualified = dict(record)
    qualified['receipt_commitment'] = 'QUALIFIED'
    qualified['receipt_qualified'] = True
    qualified['provisional_sha256'] = digest
    qblob = json.dumps(qualified, separators=(',', ':')).encode() + b'\n'
    try:
        durable(qblob)
    except BaseException as exc:
        sol076_launch_error(item, exc, 'receipt_commit')
        mark_fault()
        append_end('RECEIPT_COMMIT_UNCONFIRMED')
        raise
    record['receipt_commitment'] = 'QUALIFIED'
    record['receipt_qualified'] = True
    record['provisional_sha256'] = digest
    return record


def sol076_receipt_stream_ok(receipt_fd):
    """Caller-valid positive lines are qualified rows whose provisional witness was not ended."""
    size = os.fstat(receipt_fd).st_size
    require(0 <= size <= 1048576, 'RECEIPT_STREAM_CAP')
    raw = os.pread(receipt_fd, size, 0) if size else b''
    require(len(raw) == size, 'RECEIPT_STREAM_READ')
    if not raw:
        return True
    require(raw.endswith(b'\n'), 'RECEIPT_STREAM_PARTIAL')
    invalidated = set()
    stream_ended = False
    qualified = {}
    provisional = {}
    for line in raw.split(b'\n')[:-1]:
        require(line, 'RECEIPT_STREAM_EMPTY_LINE')
        try:
            row = json.loads(line.decode('utf-8'))
        except Exception:
            require(False, 'RECEIPT_STREAM_LINE')
        require(type(row) is dict, 'RECEIPT_STREAM_ROW')
        commitment = row.get('receipt_commitment')
        witness = row.get('provisional_sha256')
        if commitment == 'END_UNCONFIRMED':
            if witness is None:
                stream_ended = True
            else:
                invalidated.add(witness)
            continue
        if commitment == 'PROVISIONAL':
            provisional[hashlib.sha256(line + b'\n').hexdigest()] = row
            continue
        if commitment == 'QUALIFIED':
            require(type(witness) is str and witness in provisional, 'RECEIPT_QUALIFIED_WITNESS')
            qualified[witness] = row
            continue
        require(False, 'RECEIPT_STREAM_COMMITMENT')
    for digest, row in provisional.items():
        ended = stream_ended or digest in invalidated
        if row.get('body_complete') is True:
            require(not ended, 'RECEIPT_ENDED_POSITIVE')
            later = qualified.get(digest)
            require(type(later) is dict and later.get('receipt_qualified') is True and
                    later.get('body_complete') is True, 'RECEIPT_PROVISIONAL_NOT_TERMINAL')
    for digest, row in qualified.items():
        if row.get('body_complete') is True:
            require(not stream_ended and digest not in invalidated, 'RECEIPT_ENDED_QUALIFIED_POSITIVE')
    return True


def run_wave(entries, out, context, deadline, receipt_fd, results, worker_fn=worker):
    active = {}
    next_index = 0
    peak = 0
    fatal = None
    reserved = 0
    charged = 0
    launch_journal = []
    try:
        while active or next_index < len(entries):
            if STOP or time.monotonic() >= deadline:
                fatal = "OWNER_STOP" if STOP else "WALL_TIMEOUT"
            out.check()
            while not fatal and len(active) < 4 and next_index < len(entries):
                entry = entries[next_index]
                next_index += 1
                require(entry["relative_path"] not in results, "ROUTE_REUSED")
                reserved += entry["cap"]
                require(reserved <= LIMITS["transfer_body_bytes_max"], "BODY_RESERVATION")
                start = time.monotonic()
                started = now()
                item = sol076_launch_intent({'entry': entry, 'body': None,
                    'pipe': None, 'write_pipe': None, 'pidfd': None, 'pid': None,
                    'lifecycle': 'PENDING', 'start': start, 'started': started,
                    'connect_deadline': start + 15, 'deadline': min(start + entry['seconds'], deadline),
                    'buffer': b'', 'events': [], 'connected': False, 'eof': False, 'reaped': False})
                launch_journal.append(item)
                try:
                    body_fd = sol076_own_fd(item, 'body', out.create(entry['relative_path']))
                    read_fd, write_fd = os.pipe()
                    sol076_own_fd(item, 'pipe', read_fd)
                    sol076_own_fd(item, 'write_pipe', write_fd)
                    plane = a201_fork_plane(entry)
                    fork_operation = os.fork
                    item['fork_is_stock'] = (type(fork_operation) is type(time.monotonic) and
                                             getattr(fork_operation, '__module__', None) == 'posix')
                    item['fork_attempted'] = True
                    pid = fork_operation()
                except BaseException as exc:
                    sol076_launch_error(item, exc, 'acquire_plane_or_fork')
                    record = sol076_finish_record(item, {'state': 'LAUNCH_FAILED', 'body_complete': False,
                        'relative_path': entry['relative_path'], 'url': entry['url'], 'wire_bytes': None})
                    results[entry['relative_path']] = record
                    try:
                        sol076_publish_receipt(receipt_fd, record, item)
                    except BaseException:
                        record['launch'] = sol076_launch_DATA(item)
                    raise
                if pid == 0:
                    try:
                        keep = a201_bind_fork_plane(plane, body_fd, write_fd)
                        # Remove all inherited writable/readable project descriptors.
                        for descriptor in os.listdir("/proc/self/fd"):
                            fd = int(descriptor)
                            if fd not in keep:
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass  # listdir's own already-closed descriptor
                        worker_fn(entry, body_fd, write_fd, context)
                        os._exit(0)
                    except BaseException:
                        os._exit(70)
                item.update(pid=pid, lifecycle='LIVE')
                active[pid] = item  # Before close/pidfd/nonblocking can fail.
                sol076_close_slot(item, 'write_pipe')
                require(not item['sol076']['cleanup_fault'], 'LAUNCH_WRITE_CLOSE_UNCONFIRMED')
                try:
                    pidfd = sol076_own_fd(item, 'pidfd', os.pidfd_open(pid))
                except BaseException as exc:
                    sol076_launch_error(item, exc, 'pidfd_open')
                    raise
                os.set_blocking(read_fd, False)
                peak = max(peak, len(active))
            if active:
                readable, _, _ = select.select([x["pipe"] for x in active.values()], [], [], 0.05)
                resident = process_status(os.getpid())
                for pid, item in list(active.items()):
                    try:
                        resident += process_status(pid)
                    except FileNotFoundError:
                        pass
                    if item["pipe"] in readable and not item["eof"]:
                        data = os.read(item["pipe"], 131073)
                        item["buffer"] += data
                        require(len(item["buffer"]) <= 131072, "EVENT_PIPE_CAP")
                        item["eof"] = not data
                        events,item["buffer"]=sol069_events(item["buffer"],item["eof"])
                        for event in events:
                            item["events"].append(event)
                            require(len(item["events"])<=2,"EVENT_COUNT")
                            if event.get("event")=="CONNECTED":
                                require(not item["connected"],"DUPLICATE_CONNECTED")
                                item["connected"]=True
                    if resident > LIMITS["rss_bytes_max"]:
                        fatal = "RSS_CAP"
                    timed_out = time.monotonic() >= item["deadline"] or (
                        not item["connected"] and time.monotonic() >= item["connect_deadline"])
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if not done and not timed_out and not fatal:
                        continue
                    confirmed = True
                    if not done:
                        confirmed, status = stop_owned(pid, item["pidfd"], deadline)
                    if not confirmed:
                        fatal = "STOP_UNCONFIRMED"
                    item["reaped"] = confirmed
                    item['lifecycle'] = 'REAPED' if confirmed else 'STOP_UNCONFIRMED'
                    # Drain only after confirmed exit; bounded nonblocking reads.
                    if confirmed:
                        for _ in range(3):
                            try:
                                data = os.read(item["pipe"], 131073)
                            except BlockingIOError:
                                break
                            if not data:
                                break
                            item["buffer"] += data
                            require(len(item["buffer"]) <= 131072, "EVENT_PIPE_CAP")
                        events,item["buffer"]=sol069_events(item["buffer"],True)
                        item["events"].extend(events)
                    require(len(item["events"]) <= 2, "EVENT_COUNT")
                    final = [e for e in item["events"] if e.get("event") == "FINAL"]
                    count, sha, _ = digest_fd(item["body"], item["entry"]["cap"])
                    known = confirmed and not timed_out and not fatal and os.waitstatus_to_exitcode(status) == 0 and len(final) == 1
                    record = dict(final[0]) if known else {"state": "UNKNOWN_PARTIAL", "body_complete": False,
                              "failure": fatal or ("REQUEST_TIMEOUT" if timed_out else "WORKER_INCOMPLETE")}
                    if known:
                        require(record["bytes"] == count and record["sha256"] == sha,
                                "WORKER_ACCOUNTING_DRIFT")
                        if record.get("body_complete"):
                            tls = record.get("tls") or {}
                            require(record.get("http_status") == 200 and
                                    tls.get("hostname") == item["entry"]["host"] and
                                    tls.get("certificate_and_hostname_verified") is True and
                                    tls.get("verify_mode") == "CERT_REQUIRED" and
                                    tls.get("protocol") in ("TLSv1.2", "TLSv1.3") and
                                    re.fullmatch(r"[0-9a-f]{64}", tls.get("peer_certificate_sha256", "")),
                                    "PARENT_TLS_RECEIPT")
                            require(item["entry"]["sha256"] is None or
                                    (count == item["entry"]["size"] and sha == item["entry"]["sha256"] and
                                     record["state"] == "HASH_MATCH"), "PARENT_ARCHIVE_PIN")
                            require(item["entry"]["sha256"] is not None or
                                    record["state"] == "UNACCEPTED_UNPINNED_BODY", "UNPINNED_ACCEPTANCE")
                    complete = known and record.get("body_complete") is True
                    st = file_stat(item["body"], True)
                    record.update(url=item["entry"]["url"], relative_path=item["entry"]["relative_path"],
                        expected_bytes=item["entry"]["size"], expected_sha256=item["entry"]["sha256"],
                        observed_retained_bytes=count, observed_retained_sha256=sha,
                        accounting_charged_bytes=count if complete else item["entry"]["cap"],
                        unknown_body_or_wire_bytes=None, wire_bytes=None, started_msk=item["started"],
                        completed_msk=now(), elapsed_sec=round(time.monotonic() - item["start"], 6),
                        worker_exit_status=status, stop_confirmed=confirmed,
                        file={"mode": "0600", "uid": st.st_uid, "gid": st.st_gid, "nlink": st.st_nlink,
                              "device": st.st_dev, "inode": st.st_ino})
                    sol076_finish_record(item, record)
                    charged += record['accounting_charged_bytes']
                    if item['sol076']['cleanup_fault']: fatal = 'LAUNCH_CLEANUP_UNCONFIRMED'
                    results[item["entry"]["relative_path"]] = record
                    del active[pid]
                    sol076_publish_receipt(receipt_fd, record, item)
                    out.check()
            if fatal and not active:
                break
        for entry in entries[next_index:]:
            results[entry["relative_path"]] = {"state": "NOT_RUN", "failure": fatal, "url": entry["url"]}
        return {"peak_workers": peak, "reserved_body_bytes": reserved,
                "charged_body_bytes": charged, "fatal": fatal,
                "launch_journal": [sol076_launch_DATA(child) for child in launch_journal]}
    finally:
        for pid, item in active.items():
            confirmed = item["reaped"]
            if not confirmed:
                try: confirmed, _ = stop_owned(pid, item["pidfd"], deadline)
                except BaseException as exc:
                    sol076_launch_error(item, exc, 'final_stop'); confirmed = False
            item['lifecycle'] = 'REAPED' if confirmed else 'STOP_UNCONFIRMED'
            record = {"state": "UNKNOWN_PARTIAL",
                "failure": "STOP_UNCONFIRMED" if not confirmed else "CONTOUR_ABORTED",
                "accounting_charged_bytes": item["entry"]["cap"], "body_complete": False}
            results[item['entry']['relative_path']] = sol076_finish_record(item, record)


def mapping_checks(out, bill, results):
    required = ("metadata/playwright/browsers.json", "metadata/playwright/cft-version.json",
                "metadata/playwright/registry-index.ts")
    require(all(results.get(p, {}).get("body_complete") is True for p in required), "MAPPING_NOT_FETCHED")
    browsers = inert_json(out.read(required[0], 65536), 65536)
    records = browsers["browsers"]
    for wanted in bill["browser_archives"]:
        found = [r for r in records if r.get("name") == wanted["name"]]
        require(len(found) == 1 and found[0].get("revision") == wanted["revision"], "BROWSER_REVISION_MAP")
        if "browser_version" in wanted:
            require(found[0].get("browserVersion") == wanted["browser_version"], "BROWSER_VERSION_MAP")
    cft = inert_json(out.read(required[1], 65536), 65536)
    require(cft["version"] == "149.0.7827.55", "CFT_VERSION")
    for key, row in zip(("chrome", "chrome-headless-shell"), bill["browser_archives"][:2]):
        found = [r for r in cft["downloads"][key] if r.get("platform") == "linux64"]
        require(len(found) == 1 and found[0].get("url") == row["url"], "CFT_ROUTE")
    # Parse only named constant containers as inert tokens; ignore comments/template text.
    source = out.read(required[2], 262144).decode("utf-8", "strict")
    tokens = []
    lexer = re.compile(r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|[A-Za-z_$][A-Za-z0-9_$]*|\S", re.S)
    for match in lexer.finditer(source):
        token = match.group()
        if token.startswith(("//", "/*")):
            continue
        tokens.append(token)
    require(len(tokens) <= 100000, "SOURCE_TOKEN_CAP")

    def container(name, opening, closing):
        found = [i for i, token in enumerate(tokens) if token == name and i and tokens[i - 1] == "const"]
        require(len(found) == 1, "SOURCE_CONSTANT_" + name)
        start = found[0]
        equals = next((i for i in range(start + 1, min(start + 256, len(tokens))) if tokens[i] == "="), None)
        require(equals is not None and tokens[equals + 1] == opening, "SOURCE_CONTAINER_" + name)
        depth = 0
        for i in range(equals + 1, len(tokens)):
            depth += tokens[i] == opening
            depth -= tokens[i] == closing
            if depth == 0:
                return tokens[equals + 2:i]
        raise Refused("SOURCE_UNCLOSED_CONTAINER")

    mirrors = container("PLAYWRIGHT_CDN_MIRRORS", "[", "]")
    require("'https://cdn.playwright.dev/dbazure/download/playwright'" in mirrors or
            '"https://cdn.playwright.dev/dbazure/download/playwright"' in mirrors, "PLAYWRIGHT_CDN_MAP")
    routes = container("DOWNLOAD_PATHS", "{", "}")
    for key, route in (("chromium", "builds/cft/%s/linux64/chrome-linux64.zip"),
                       ("chromium-headless-shell", "builds/cft/%s/linux64/chrome-headless-shell-linux64.zip"),
                       ("ffmpeg", "builds/ffmpeg/%s/ffmpeg-linux.zip")):
        depth = 0
        maps = []
        for i, token in enumerate(routes):
            if depth == 0 and token in ("'" + key + "'", '"' + key + '"') and routes[i + 1:i + 3] == [":", "{"]:
                end = i + 3
                while end < len(routes) and routes[end] != "}":
                    require(routes[end] != "{", "SOURCE_NESTED_ROUTE")
                    end += 1
                require(end < len(routes), "SOURCE_ROUTE_UNCLOSED")
                maps.append(routes[i + 3:end])
            depth += token == "{"
            depth -= token == "}"
        require(len(maps) == 1, "PLAYWRIGHT_ROUTE_OBJECT_" + key)
        matched = []
        for i in range(len(maps[0]) - 2):
            token = maps[0][i]
            if token.startswith(("'", '"')) and token.endswith(token[0]) and re.fullmatch(
                    r"linux-x64|ubuntu(?:20\.04|22\.04|24\.04|26\.04)-x64|debian(?:11|12|13)-x64", token[1:-1]):
                require(maps[0][i + 1] == ":", "SOURCE_PLATFORM_VALUE")
                value = maps[0][i + 2]
                matched.append(value in ("'" + route + "'", '"' + route + '"'))
        require(matched and all(matched), "PLAYWRIGHT_ROUTE_" + key)
    return {"state": "INERT_MAPPING_MATCH", "archive_publisher_digest_authority": False}


def resources():
    with open("/proc/self/cgroup", encoding="ascii") as stream:
        membership = stream.read(65536)
    leaves = [line.split(":", 2)[2] for line in membership.splitlines() if line.startswith("0::")]
    require(len(leaves) == 1 and ".." not in leaves[0].split("/"), "CGROUP_LEAF_UNKNOWN")
    with open("/proc/self/mountinfo", encoding="ascii") as stream:
        mounts = [line.split() for line in stream.read(1048576).splitlines() if " - cgroup2 " in line]
    require(len(mounts) == 1, "CGROUP_MOUNT_UNKNOWN")
    mount_root, mount_point = mounts[0][3:5]
    require("\\" not in mount_root + mount_point and (leaves[0] == mount_root or
            leaves[0].startswith(mount_root.rstrip("/") + "/")), "CGROUP_MOUNT_MEMBERSHIP")
    leaf = mount_point + leaves[0][len(mount_root.rstrip("/")):].rstrip("/")
    require(os.path.isdir(leaf), "CGROUP_LEAF_ABSENT")
    observed = {"actual_cgroup_membership": membership, "leaf": leaf,
                "cpu_affinity": sorted(os.sched_getaffinity(0)), "pid": os.getpid()}
    def cgroup_read(path, key):
        try:
            with open(path + "/" + key, encoding="ascii") as stream:
                return stream.read(65536).strip()
        except FileNotFoundError:
            return None  # controller/limit not exposed here; never claim zero quota
    for key in ("cpu.max", "memory.max", "memory.current", "cpuset.cpus.effective", "cpu.pressure", "io.pressure"):
        observed[key] = cgroup_read(leaf, key)
    observed["cgroup.controllers"] = cgroup_read(leaf, "cgroup.controllers")
    observed["absent_leaf_fields"] = [k for k, v in observed.items() if v is None]
    require(observed["memory.max"] is not None and observed["memory.current"] is not None,
            "CGROUP_MEMORY_LIMIT_UNKNOWN")
    if observed["memory.max"] != "max":
        require(int(observed["memory.max"]) - int(observed["memory.current"]) >= 268435456,
                "CGROUP_MEMORY_SHORTFALL")
    observed["ancestor_limits"] = []
    parent = os.path.dirname(leaf)
    while parent.startswith(mount_point) and parent != leaf:
        limits = {"path": parent}
        for key in ("memory.max", "memory.current", "cpu.max"):
            limits[key] = cgroup_read(parent, key)
        if limits["memory.max"] not in (None, "max"):
            require(limits["memory.current"] is not None, "ANCESTOR_MEMORY_CURRENT_UNKNOWN")
            require(int(limits["memory.max"]) - int(limits["memory.current"]) >= 268435456,
                    "CGROUP_ANCESTOR_MEMORY_SHORTFALL")
        observed["ancestor_limits"].append(limits)
        if parent == mount_point:
            break
        parent = os.path.dirname(parent)
    available = os.statvfs("/var/tmp")
    observed["var_tmp_available_bytes"] = available.f_bavail * available.f_frsize
    require(observed["var_tmp_available_bytes"] >= 2147483648 + 67108864, "DISK_SHORTFALL")
    with open("/proc/meminfo", encoding="ascii") as stream:
        mem = stream.read(65536)
    observed["memory_available_bytes"] = int(re.search(r"^MemAvailable:\s+(\d+) kB$", mem, re.M)[1]) * 1024
    require(observed["memory_available_bytes"] >= 268435456, "MEMORY_SHORTFALL")
    return observed


def signal_stop(signum, frame):
    global STOP
    STOP = True


def execute(plan, raw, remote, local, paths, dirs):
    deadline = time.monotonic() + 1800
    observed = resources()
    require(not os.path.lexists(ROOT), "EXISTING_QUARANTINE")
    inputs = []
    out = None
    results = {}
    terminal = {"schema": "friday.astra.e4.material-acquisition-inventory.v1", "started_msk": now(),
                "plan_sha256": PLAN_SHA, "owner_sha256": OWNER_SHA, "resources": observed,
                "execution_install_root_live_gate_release_credit": False, "wire_bytes": None}
    receipt = None
    try:
        for row in local:
            fd = absolute_open(row["source"])
            try:
                digest_fd(fd, row["cap"], row["sha256"], row["size"])
            except BaseException:
                os.close(fd)
                raise
            inputs.append((row, fd, identity(file_stat(fd))))
        ca_fd = absolute_open(CA)
        try:
            digest_fd(ca_fd, 1048576, CA_SHA)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cafile="/proc/self/fd/" + str(ca_fd))
        finally:
            os.close(ca_fd)
        require(sum(row["cap"] for row in local) + len(raw) + LIMITS["transfer_body_bytes_max"] +
                67108864 < LIMITS["quarantine_bytes_max"], "QUARANTINE_RESERVATION")
        resource.setrlimit(resource.RLIMIT_AS, (67108864, 67108864))
        signal.signal(signal.SIGINT, signal_stop)
        signal.signal(signal.SIGTERM, signal_stop)
        require(not STOP and time.monotonic() < deadline, "STOP_BEFORE_EFFECTS")
        os.umask(0o077)
        out = Output(ROOT, paths, dirs)
        bill_fd = out.create("bill.json")
        write_all(bill_fd, raw)
        os.fsync(bill_fd)
        os.close(bill_fd)
        receipt = out.create("transport-receipts.ndjson")
        for row, fd, checkpoint in inputs:
            require(time.monotonic() < deadline, "COPY_WALL_TIMEOUT")
            results[row["relative_path"]] = copy_input(row, fd, out, checkpoint)
        wave_a = [r for r in remote if r["kind"] != "browser"]
        terminal["wave_a"] = run_wave(wave_a, out, context, deadline, receipt, results)
        try:
            require(not terminal["wave_a"]["fatal"], "WAVE_A_CONTROL_FAILURE")
            terminal["mapping"] = mapping_checks(out, plan["acquisition_bill"], results)
            terminal["wave_b"] = run_wave([r for r in remote if r["kind"] == "browser"],
                                           out, context, deadline, receipt, results)
        except Refused as exc:
            terminal["mapping"] = {"state": "FAIL", "failure": str(exc)}
        terminal["state"] = ("CONTOUR_ABORTED" if terminal["wave_a"]["fatal"] or
                             terminal.get("wave_b", {}).get("fatal") else
                             "ACQUISITION_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS")
        if receipt is not None:
            terminal["receipt_stream_qualified"] = sol076_receipt_stream_ok(receipt)
    except BaseException as exc:
        terminal["state"] = "CONTOUR_ABORTED"
        terminal["receipt_stream_qualified"] = False
        terminal["failure"] = str(exc)[:512] if isinstance(exc, Refused) else type(exc).__name__
        if out is None:
            raise
    finally:
        for _, fd, _ in inputs:
            try: os.close(fd)
            except BaseException as exc:
                remember_error(exc, 'G1.execute.input_close')
                terminal.update(state='CONTOUR_ABORTED', cleanup_failed=True,
                                body_complete=False, terminal_completion=False)
                terminal.setdefault('failure', type(exc).__name__)
        if receipt is not None:
            try: os.close(receipt)
            except BaseException as exc:
                remember_error(exc, 'G1.execute.receipt_close')
                terminal.update(state='CONTOUR_ABORTED', cleanup_failed=True,
                                body_complete=False, terminal_completion=False,
                                receipt_stream_qualified=False)
                terminal.setdefault('failure', type(exc).__name__)
                for record in results.values():
                    if 'launch' in record:
                        record['body_complete'] = False
                        record['terminal_completion'] = False
                        record['acceptance_complete'] = False
                        record['receipt_commitment'] = 'PUBLICATION_FAULT'
                        record['receipt_qualified'] = False
                if out is not None and out.root is not None:
                    end_fd = None
                    try:
                        end_fd = os.open('transport-receipts.ndjson', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=out.root)
                        end_line = json.dumps({'schema': 'friday.sol076.receipt-end.v1',
                            'receipt_commitment': 'END_UNCONFIRMED', 'receipt_qualified': False,
                            'finite_end': 'RECEIPT_CLOSE_UNCONFIRMED'}, separators=(',', ':')).encode() + b'\n'
                        write_all(end_fd, end_line)
                        os.fsync(end_fd)
                    except BaseException as end_exc:
                        remember_error(end_exc, 'G1.execute.receipt_end')
                    finally:
                        if end_fd is not None:
                            try: os.close(end_fd)
                            except BaseException as close_exc:
                                remember_error(close_exc, 'G1.execute.receipt_end_close')
    require(out is not None, "NO_QUARANTINE")
    if any(wave.get("fatal") == "STOP_UNCONFIRMED" for wave in (terminal.get("wave_a", {}), terminal.get("wave_b", {}))) or "STOP_UNCONFIRMED" in terminal.get("failure", "") or any(
            record.get('launch', {}).get('lifecycle') in ('STOP_UNCONFIRMED', 'BIRTH_UNCONFIRMED') for record in results.values()):
        out.close()
        return {"state": "STOP_UNCONFIRMED", "inventory": None,
                "quarantine": ROOT, "acceptance_complete": False, 'materials': results,
                'body_complete': False, 'terminal_completion': False,
                "failure": "Owned worker stop not confirmed; no terminal completion receipt created."}
    if terminal.get('cleanup_failed') or terminal.get('receipt_stream_qualified') is False or any('launch' in record and (
            record['launch']['birth'] != 'CREATED' or not record['launch']['ordinary_cleanup_complete'])
            for record in results.values()) or any(
            record.get('body_complete') is True and 'launch' in record and record.get('receipt_qualified') is not True
            for record in results.values()):
        try: out.close()
        except BaseException as exc: remember_error(exc, 'G1.execute.failed_launch.output_close')
        return {'state': 'CONTOUR_ABORTED', 'inventory': None, 'materials': results,
                'body_complete': False, 'terminal_completion': False, 'acceptance_complete': False,
                'failure': terminal.get('failure', 'ORDINARY_LAUNCH_OR_CLEANUP_FAILED')}
    try:
        out.check()
        for row in remote + local:
            results.setdefault(row["relative_path"], {"state": "NOT_RUN", "failure": terminal.get("failure", "MAPPING_OR_STOP")})
        # Exact declared slots are retained even when NOT_RUN; no inferred transfers.
        for path in paths:
            if path not in out.created:
                os.close(out.create(path))
        terminal["materials"] = results
        terminal["completed_msk"] = now()
        terminal["actual_regular_files"] = len(out.created)
        terminal["actual_directories_including_root"] = len(out.fds)
        terminal["retained_directories"] = [{"path": key or ".", "type": "directory", "mode": "0700",
                    "uid": os.fstat(fd).st_uid, "gid": os.fstat(fd).st_gid,
                    "nlink": os.fstat(fd).st_nlink, "device": os.fstat(fd).st_dev,
                    "inode": os.fstat(fd).st_ino} for key, fd in out.fds.items()]
        terminal["retained_files"] = []
        for path in paths:
            parts = relpath(path)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=out.fds["/".join(parts[:-1])])
            try:
                st = file_stat(fd, True)
                count, sha, _ = digest_fd(fd, LIMITS["quarantine_bytes_max"])
                terminal["retained_files"].append({"path": path, "type": "regular", "mode": "0600",
                    "uid": st.st_uid, "gid": st.st_gid, "nlink": st.st_nlink, "device": st.st_dev,
                    "inode": st.st_ino, "bytes": count if path != "inventory.json" else None,
                    "sha256": sha if path != "inventory.json" else None,
                    "self_receipt": "FINAL_HASH_PRINTED_TO_OWNER_STDOUT" if path == "inventory.json" else None})
            finally:
                os.close(fd)
        terminal["body_bytes_retained"] = sum(r.get("observed_retained_bytes", 0) for r in results.values())
        terminal["body_bytes_unknown_reserved"] = sum(r.get("accounting_charged_bytes", 0) for r in results.values()
                                                      if not r.get("body_complete", False))
        terminal["body_bytes_conservatively_charged"] = sum(r.get("accounting_charged_bytes", 0) for r in results.values())
        require(terminal["body_bytes_conservatively_charged"] <= LIMITS["transfer_body_bytes_max"], "FINAL_BODY_CAP")
        data = json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        require(len(data) <= 33554432, "INVENTORY_CAP")
        require(sum(r["bytes"] or 0 for r in terminal["retained_files"]) + len(data) <=
                LIMITS["quarantine_bytes_max"], "FINAL_QUARANTINE_CAP")
        fd = os.open("inventory.json", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=out.root)
        try:
            require(os.fstat(fd).st_size == 0, "INVENTORY_COLLISION")
            file_stat(fd, True)
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        out.check()
        return {"inventory": ROOT + "/inventory.json", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "state": terminal["state"],
                "acceptance_complete": False}
    finally:
        out.close()


def main():
    plan, raw = pinned_json(PLAN, PLAN_SHA)
    owner, _ = pinned_json(OWNER, OWNER_SHA)
    require(owner["schema"] == "friday.astra.e4.owner-delegated-project-authority.v1", "OWNER_SCHEMA")
    remote, local, paths, dirs = compile_plan(plan)
    if len(sys.argv) == 1 or sys.argv[1:] == ["--describe"]:
        print(json.dumps({"network_effects": False, "quarantine_effects": False,
              "remote_routes": len(remote), "local_copies": len(local), "files": len(paths),
              "directories_including_root": len(dirs) + 1, "limits": LIMITS}))
        return
    require(len(sys.argv) == 3 and sys.argv[1] == "--execute-reviewed-a023", "CLI_REVIEW_REQUIRED")
    fd = absolute_open(os.path.abspath(__file__))
    try:
        digest_fd(fd, 1048576, sys.argv[2])
    finally:
        os.close(fd)
    print(json.dumps(execute(plan, raw, remote, local, paths, dirs)))


"""SOL069 inert Source. One bounded full frame; never a prefix/hash body.
Representation limits below are NOT proofs of fit for all required stock inputs.
No filesystem backing, grant, channel, role, clock, or cap is added.
"""
SOL069_EXPANDED_BYTES = 16 * 1048576

"""SOL069 inert Source. One bounded full frame; never a prefix/hash body.
Representation limits below are NOT proofs of fit for all required stock inputs.
No filesystem backing, grant, channel, role, clock, or cap is added.
"""
SOL069_EXPANDED_BYTES = 16 * 1048576

def sol069_runs(raw):
    out=bytearray();at=0
    while at<len(raw):
        end=at+1
        while end<len(raw) and raw[end]==raw[at]:end+=1
        # Same raw/RLE choice, without allocating an abandoned last run.
        if len(out)+5>=len(raw):return 'raw',raw
        out.extend((end-at).to_bytes(4,'big'));out.append(raw[at]);at=end
    return 'rle',bytes(out)

def sol069_encode(value,cap,check):
    # Necessary complete-wire lower bound, not allocator/RSS qualification.
    # The fixed empty header is 45 bytes without its root expression.
    # Actual final complete JSON/frame validation remains mandatory.
    lower=53
    check(lower<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
    nodes=[];identities={};bodies=[];body_ids={};expanded=0
    def reserve(amount):
        nonlocal lower
        check(lower+amount<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
        lower+=amount
    def digits_floor(number):
        return max(1,number.bit_length()//4)
    def reference(index):
        reserve(6+digits_floor(index))
        return ['r',index]
    def body(raw):
        nonlocal expanded
        if raw in body_ids:return body_ids[raw]
        expanded+=len(raw)
        check(expanded<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
        # 112 is the zero-width body descriptor with its complete SHA256.
        # Both raw/rle tags have three characters. Larger integer widths
        # only increase the exact final representation, never this floor.
        reserve(112+(1 if bodies else 0))
        codec,stored=sol069_runs(raw)
        reserve(len(stored))
        index=len(bodies);body_ids[raw]=index
        bodies.append(({'bytes':len(raw),'stored':len(stored),'codec':codec,
            'sha256':hashlib.sha256(raw).hexdigest()},stored))
        return index
    def visit(row):
        kind=type(row)
        if row is None or kind in (bool,int,float):
            if row is None:width=4
            elif kind is bool:width=4 if row else 5
            elif kind is int:width=digits_floor(row)+(1 if row<0 else 0)
            else:width=1
            reserve(6+width)
            return ['v',row]
        check(kind in (str,bytes,bytearray,list,dict),'SOL069_DATA_TYPE')
        key=id(row)
        if key in identities:return reference(identities[key])
        # Empty node spellings are necessary wire floors. Debit before
        # allocating the node, identity entry or its nested item cells.
        reserve({str:23,bytes:25,bytearray:29,list:26,dict:26}[kind]+
            (1 if nodes else 0))
        index=len(nodes);identities[key]=index;node={'kind':kind.__name__};nodes.append(node)
        if kind in (str,bytes,bytearray):
            # A single complete body cannot exceed the ORIGINAL expanded
            # bound, even when later equal bodies are deduplicated. Count
            # UTF8 bytes before encode; exact stock types invoke no formatter.
            check(len(row)<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
            if kind is str:
                width=0
                for char in row:
                    point=ord(char)
                    width+=1 if point<128 else 2 if point<2048 else 3 if point<65536 else 4
                    check(width<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
                raw=row.encode('utf8','surrogatepass')
            else:raw=bytes(row)
            node['body']=body(raw)
        elif kind is list:
            items=[];node['items']=items
            for item in row:
                if items:reserve(1)
                items.append(visit(item))
        else:
            check(all(type(k) is str for k in row),'SOL069_DATA_KEYS')
            items=[];node['items']=items
            for k,v in row.items():
                reserve(3+(1 if items else 0))
                left=visit(k);right=visit(v)
                items.append([left,right])
        return reference(index)
    root=visit(value)
    head=json.dumps({'root':root,'nodes':nodes,'bodies':[r[0] for r in bodies],
        'expanded':expanded},separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    check(8+len(head)+sum(len(r[1]) for r in bodies)<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
    return b'DS69'+len(head).to_bytes(4,'big')+head+b''.join(r[1] for r in bodies)

def sol069_decode(raw,cap,check,parse):
    physical = type(raw) is bytes and raw[:8] == A201_REF_MAGIC
    raw = a201_resolve_body(raw)
    check(type(raw) is bytes and 8<=len(raw)<=(A201_BODY_CAP if physical else cap) and raw[:4]==b'DS69','SOL069_FULL_FRAME')
    size=int.from_bytes(raw[4:8],'big');check(0<size<=len(raw)-8,'SOL069_HEADER_LENGTH')
    head=parse(raw[8:8+size]);at=8+size
    check(type(head) is dict and set(head)=={'root','nodes','bodies','expanded'} and
        type(head['expanded']) is int and 0<=head['expanded']<=SOL069_EXPANDED_BYTES and
        type(head['nodes']) is list and type(head['bodies']) is list,'SOL069_TYPED_HEADER')
    blobs=[];expanded=0
    for row in head['bodies']:
        check(type(row) is dict and set(row)=={'bytes','stored','codec','sha256'} and
            type(row['bytes']) is type(row['stored']) is int and row['bytes']>=0 and
            0<=row['stored']<=len(raw)-at,'SOL069_COMPLETE_BODY_LENGTH')
        expanded+=row['bytes'];check(expanded<=head['expanded'],'SOL069_EXPANDED_SUM')
        chunk=raw[at:at+row['stored']];at+=row['stored']
        check(row['codec'] in ('raw','rle'),'SOL069_CODEC')
        if row['codec']=='rle':
            check(len(chunk)%5==0,'SOL069_RUN_LENGTH');value=bytearray();previous=None
            for pos in range(0,len(chunk),5):
                count=int.from_bytes(chunk[pos:pos+4],'big');byte=chunk[pos+4]
                check(count>0 and byte!=previous and len(value)+count<=row['bytes'],'SOL069_CANONICAL_RUN')
                value.extend(bytes((byte,))*count);previous=byte
            chunk=bytes(value)
        check(len(chunk)==row['bytes'] and hashlib.sha256(chunk).hexdigest()==row['sha256'],
            'SOL069_COMPLETE_BODY_PREIMAGE_SHA')
        blobs.append(chunk)
    check(at==len(raw) and expanded==head['expanded'],'SOL069_COMPLETE_FRAME_NO_TRAILER')
    values=[]
    for node in head['nodes']:
        check(type(node) is dict and node.get('kind') in ('str','bytes','bytearray','list','dict'),
            'SOL069_NODE_KIND')
        kind=node['kind']
        if kind in ('list','dict'):
            check(set(node)=={'kind','items'} and type(node['items']) is list,'SOL069_CONTAINER')
            values.append([] if kind=='list' else {})
        else:
            check(set(node)=={'kind','body'} and type(node['body']) is int and
                0<=node['body']<len(blobs),'SOL069_BODY_REF')
            value=blobs[node['body']]
            values.append(value.decode('utf8','surrogatepass') if kind=='str' else
                bytearray(value) if kind=='bytearray' else value)
    def reference(row):
        check(type(row) is list and len(row)==2,'SOL069_VALUE_REFERENCE')
        tag,index=row
        if tag=='v':
            check(index is None or type(index) in (bool,int,float),'SOL069_SCALAR');return index
        check(tag=='r' and type(index) is int and 0<=index<len(values),'SOL069_NODE_REFERENCE')
        return values[index]
    for index,node in enumerate(head['nodes']):
        if node['kind']=='list':values[index].extend(reference(row) for row in node['items'])
        elif node['kind']=='dict':
            for pair in node['items']:
                check(type(pair) is list and len(pair)==2,'SOL069_KEY_VALUE')
                key=reference(pair[0]);check(type(key) is str and key not in values[index],
                    'SOL069_DUPLICATE_KEY');values[index][key]=reference(pair[1])
    return reference(head['root'])

def sol069_copy(value,memo=None):
    if value is None or type(value) in (str,bytes,int,bool,float):return value
    if memo is None:memo={}
    key=id(value)
    if key in memo:return memo[key]
    if type(value) is bytearray:
        result=bytearray(value);memo[key]=result;return result
    if type(value) is list:
        result=[];memo[key]=result;result.extend(sol069_copy(v,memo) for v in value);return result
    if type(value) is dict:
        result={};memo[key]=result
        for k,v in value.items():result[k]=sol069_copy(v,memo)
        return result
    raise TypeError('SOL069_DATA_COPY_TYPE')

def sol069_equal(left,right):
    """Type/value equality PLUS a bijection of mutable body identities.
    No receiver grants origin authority by Python object address alone.
    """
    work=[(left,right)];seen=set();forward={};reverse={}
    while work:
        a,b=work.pop()
        if type(a) is not type(b):return False
        if type(a) in (dict,list,bytearray):
            x,y=id(a),id(b)
            if x in forward and forward[x]!=y or y in reverse and reverse[y]!=x:return False
            forward[x]=y;reverse[y]=x
            if (x,y) in seen:continue
            seen.add((x,y))
        if type(a) is dict:
            if set(a)!=set(b):return False
            work.extend((a[k],b[k]) for k in a)
        elif type(a) is list:
            if len(a)!=len(b):return False
            work.extend(zip(a,b))
        elif type(a) is float:
            if struct.pack('!d',a)!=struct.pack('!d',b):return False
        elif a!=b:return False
    return True


PACKET_CAP = 1048576
CAP_PROBE_BYTES = PACKET_CAP + 1
CONSTANT_RUN_STORED = 5
WORKER_EVENT_TOTAL = 131072
WORKER_LENGTH_PREFIX = 4
WORKER_LITERAL_BODY_MIN_FAILURE = 131069

def sol069_worker_literal_exceeds(body_len):
    return WORKER_LENGTH_PREFIX + body_len > WORKER_EVENT_TOTAL

def sol069_literal_frame_exceeds(body_len, header_len, cap):
    return 8 + header_len + body_len > cap

def sol069_preowned_backing(frame):
    """Complete Source frame; physical only in exact admitted native binding.
    A generic same-pid/local fallback NEVER claims outside-owner custody.
    """
    if type(frame) is not bytes:
        raise TypeError("SOL069_PREOWNED_FRAME")
    bound = a201_physical_bound()
    reference = a201_store_body(frame) if bound else None
    return {"backing": frame, "view": memoryview(frame).toreadonly(), "length": len(frame),
            "physical_reference": reference, "physical_frame_before_channel_write": bound,
            "complete_native_error_custody": False, "bound_before_channel_write": True,
            "receiver_accepted": False, "pipe_accepted": False, "receipt_accepted": False,
            "both_accepted": False, "exit_is_handover": False, "eof_is_handover": False,
            "digest_is_handover": False, "pending_is_handover": False}

def sol069_commit_existing(fd, frame, cap, write, pread, pwrite, fstat, isreg):
    """Give the complete frame to an fd the caller already owns.
    A regular file is written at offset 0 and read back.
    A full pipe write still leaves acceptance with that existing reader.
    A short write leaves the unwritten tail uncommitted.
    """
    if type(frame) is not bytes or len(frame) == 0 or len(frame) > cap:
        raise RuntimeError("SOL069_COMMIT_CAP")
    # Generic terminal receivers expect ORIGINAL JSON/packet bytes. Physical
    # shadow is independently preheld; a reference is only sent by the
    # explicit worker event path whose actual decoder consumes it.
    if a201_physical_bound():
        a201_store_body(frame)
    st = fstat(fd)
    if isreg(st.st_mode):
        pwrite(fd, frame, 0)
        held = pread(fd, len(frame), 0)
        st2 = fstat(fd)
        if held != frame or st2.st_size != len(frame):
            raise RuntimeError("SOL069_COMMIT_READBACK")
        return "REGULAR_EXISTING_READBACK"
    sent = 0
    while sent < len(frame):
        count = write(fd, frame[sent:])
        if type(count) is not int or count <= 0:
            raise RuntimeError("SOL069_COMMIT_SHORT")
        sent += count
    return "PIPE_FULL_WRITE_ACCEPTANCE_REMAINS_WITH_EXISTING_READER"


# A201: physical Source-only frame custody. This is a transport/body mechanism,
# NOT acceptance of original native/error objects or a whole resource proof.
A201_BODY_MAGIC = b"FRBOD201"
A201_REF_MAGIC = b"FRREF201"
A201_BODY_CAP = SOL069_EXPANDED_BYTES
A201_BODY_HEADER = 128
A201_BODY_FD = 131
A201_BODY_STATE = {}
A201_BODY_VIEWS = {}
A201_PREFIX = struct.Struct("<8sIIQQ32s32s")
A201_REFERENCE = struct.Struct("<8sIIQQQ32s")
A201_RECORD = struct.Struct("<QQ32s")

def a201_guard(end_ns):
    if type(end_ns) is not int or time.monotonic_ns() >= end_ns:
        raise RuntimeError("A201_ORIGINAL_MINIMUM_END_NO_RENEWAL")

def a201_capsule_binding():
    raw = os.pread(100, 849, 0)
    if len(raw) != 848 or raw[:8] != b"FRA061C1":
        raise RuntimeError("A201_ACTUAL_CAPSULE_BINDING")
    # cap v1: magic + six uint32 + ten uint64; session is its first32 body.
    start_ns, work_ns, hard_ns = struct.unpack_from("<QQQ", raw, 32)
    if not start_ns < work_ns < hard_ns:
        raise RuntimeError("A201_ORIGINAL_CAPSULE_CLOCK")
    a201_guard(hard_ns)
    return raw[112:144], hashlib.sha256(raw).digest(), hard_ns

class A201BodyView:
    def __init__(self, fd, slot, session, capsule_sha, end_ns, capacity=A201_BODY_CAP):
        import mmap
        if type(slot) is not int or not 0 <= slot < 4 or len(session) != 32 or len(capsule_sha) != 32:
            raise RuntimeError("A201_BODY_EXPECTED_BINDING")
        a201_guard(end_ns)
        self.end_ns = end_ns
        st = os.fstat(fd)
        required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        if not stat.S_ISREG(st.st_mode) or st.st_size != A201_BODY_HEADER + capacity or (
                fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required):
            raise RuntimeError("A201_BODY_PHYSICAL_EXTENT_SEALS")
        self.fd, self.slot, self.session, self.capsule_sha = fd, slot, session, capsule_sha
        self.capacity, self.identity = capacity, (st.st_dev, st.st_ino)
        self.expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
        self.prefix = os.pread(fd, A201_BODY_HEADER, 0)
        if len(self.prefix) != A201_BODY_HEADER or self.prefix[:96] != self.expected:
            raise RuntimeError("A201_BODY_PREFIX_CORRESPONDENCE")
        # Strong readonly descriptor is owned BEFORE the actual bounded birth.
        # Mapping is bounded to each exact record, not four simultaneous16M maps.
        self.mmap = mmap
        self.reads = 0
        self.actual_body_custody = True
        self.complete_native_error_custody = False
        self.records = []

    def read(self, reference):
        a201_guard(self.end_ns)
        if type(reference) is not bytes or len(reference) != A201_REFERENCE.size:
            raise RuntimeError("A201_REFERENCE_SIZE")
        magic, version, slot, sequence, offset, length, sha = A201_REFERENCE.unpack(reference)
        if magic != A201_REF_MAGIC or version != 1 or slot != self.slot or sequence < 2 or sequence % 2:
            raise RuntimeError("A201_REFERENCE_GENERATION_SLOT_SEQUENCE")
        if not A201_BODY_HEADER <= offset <= A201_BODY_HEADER + self.capacity or not (
                0 < length <= A201_BODY_HEADER + self.capacity - offset):
            raise RuntimeError("A201_REFERENCE_ACTUAL_RANGE")
        before = os.fstat(self.fd)
        if (before.st_dev, before.st_ino) != self.identity or before.st_size != A201_BODY_HEADER + self.capacity:
            raise RuntimeError("A201_BODY_RECEIVER_IDENTITY")
        head = os.pread(self.fd, A201_BODY_HEADER, 0)
        self.reads += len(head)
        if len(head) != A201_BODY_HEADER or head[:96] != self.expected:
            raise RuntimeError("A201_BODY_RECEIVER_PREFIX")
        published, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
        if published < sequence or published % 2 or poison or not offset + length <= end <= (
                A201_BODY_HEADER + self.capacity):
            raise RuntimeError("A201_BODY_UNCOMMITTED_OR_POISONED")
        base = offset - offset % self.mmap.PAGESIZE
        span = offset - base + length
        view = self.mmap.mmap(self.fd, span, access=self.mmap.ACCESS_READ, offset=base)
        try:
            raw = bytes(view[offset - base:offset - base + length])
            self.reads += len(raw)
            if len(raw) != length or hashlib.sha256(raw).digest() != sha:
                raise RuntimeError("A201_FULL_PHYSICAL_BODY_SHA")
            # Appended records never overwrite a previous original body.
            tail = os.pread(self.fd, A201_BODY_HEADER, 0)
            self.reads += len(tail)
            after = os.fstat(self.fd)
            if len(tail) != A201_BODY_HEADER or tail[:96] != self.expected or (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise RuntimeError("A201_BODY_RECEIVER_DRIFT")
            seq2, end2, poison2, _ = struct.unpack_from("<QQQQ", tail, 96)
            if seq2 < sequence or end2 < offset + length or poison2:
                raise RuntimeError("A201_BODY_RECEIVER_LATE_FAILURE")
            a201_guard(self.end_ns)
            # Strong original is held by the actual consumer, not copied into
            # a second unbounded per-view lifetime bank.
            return raw
        finally:
            view.close()

def a201_create_body(slot, session, capsule_sha, end_ns, owners, capacity=A201_BODY_CAP,
                     *, source_uid=None, source_gid=None):
    a201_guard(end_ns)
    # Allocate and attach the original intention BEFORE the native acquisition.
    # A refused intention owns no fd; a returned fd enters this existing slot
    # before any initialization, seal, view or further ledger allocation.
    state = {"fd": None, "slot": slot, "capacity": capacity, "readonly_fd": None,
             "view": None, "end_ns": end_ns, "born": False,
             "complete_native_error_custody": False}
    owners.append(state)
    A201_BODY_STATE.setdefault("constructing", []).append(state)
    fd = os.memfd_create("friday-source-body-a201", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    state["fd"] = fd
    # Existing UID0 caller allocates Source-only backing, but native validator
    # requires the ORIGINAL capsule's Source UID/GID, not caller UID0. The
    # outside readonly view remains independently held by that same caller.
    source_uid = os.getuid() if source_uid is None else source_uid
    source_gid = os.getgid() if source_gid is None else source_gid
    if type(source_uid) is not int or type(source_gid) is not int or min(source_uid, source_gid) < 0:
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_TYPES")
    os.fchown(fd, source_uid, source_gid)
    os.fchmod(fd, 0o600)
    st = os.fstat(fd)
    if (st.st_uid, st.st_gid) != (source_uid, source_gid):
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_CORRESPONDENCE")
    os.ftruncate(fd, A201_BODY_HEADER + capacity)
    header = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
    header += struct.pack("<QQQQ", 0, A201_BODY_HEADER, 0, 0)
    if os.pwrite(fd, header, 0) != len(header):
        raise RuntimeError("A201_HEADER_SHORT_INITIALIZATION")
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
    state["readonly_fd"] = readonly
    state["view"] = A201BodyView(readonly, slot, session, capsule_sha, end_ns, capacity)
    A201_BODY_VIEWS[slot] = state["view"]
    return state

def a201_writer():
    import mmap
    import sys
    cached = A201_BODY_STATE.get("writer")
    if cached is not None:
        return cached
    # Four literal helper copies can execute in the SAME admitted process.
    # They must share the actual plane cursor/fault latch, not each assume the
    # same fd131 is fresh. This is one private in-process state, not a service,
    # receiver, grant, external artifact, or unbounded lifetime bank.
    shared = getattr(sys, "_friday_sol073_plane_writer", None)
    if shared is not None and shared["owner_pid"] == os.getpid():
        st = os.fstat(A201_BODY_FD)
        if shared["fd"] != A201_BODY_FD or (st.st_dev, st.st_ino) != shared["identity"]:
            raise RuntimeError("A201_SHARED_WRITER_DESCRIPTOR_DRIFT")
        A201_BODY_STATE["writer"] = shared
        return shared
    session, capsule_sha, hard_ns = a201_capsule_binding()
    fd = A201_BODY_FD
    st = os.fstat(fd)
    head = os.pread(fd, A201_BODY_HEADER, 0)
    if len(head) != A201_BODY_HEADER:
        raise RuntimeError("A201_SOURCE_BODY_HEADER")
    magic, version, slot, capacity, generation, actual_session, actual_sha = A201_PREFIX.unpack(head[:96])
    expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, A201_BODY_CAP, 1, session, capsule_sha)
    required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if head[:96] != expected or (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()) or not stat.S_ISREG(st.st_mode) or st.st_size != (
            A201_BODY_HEADER + capacity) or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required:
        raise RuntimeError("A201_SOURCE_BODY_BINDING")
    sequence, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
    if sequence or end != A201_BODY_HEADER or poison or retired:
        raise RuntimeError("A201_SOURCE_BODY_NOT_FRESH")
    cached = {"fd": fd, "slot": slot, "capacity": capacity, "end": end,
              "sequence": 0, "mapping": None, "mapping_bytes": 0,
              "identity": (st.st_dev, st.st_ino), "mmap": mmap, "end_ns": hard_ns,
              "owner_pid": os.getpid(), "complete_native_error_custody": False, "records": []}
    sys._friday_sol073_plane_writer = cached
    A201_BODY_STATE["writer"] = cached
    return cached

def a201_store_body(frame):
    if type(frame) is not bytes or not frame:
        raise RuntimeError("A201_BODY_FULL_FRAME_REQUIRED")
    state = a201_writer()
    a201_guard(state["end_ns"])
    # A failed attempt is terminal for this actual plane writer. In particular
    # an ordinary exception AFTER either publication word must not let finally
    # re-enter with an old cursor and overwrite a committed original.
    if state.get("publication_fault"):
        raise RuntimeError("A201_PRIOR_PUBLICATION_FAULT_NO_RETRY")
    if state["records"] and state["records"][-1][3] is frame:
        return state["records"][-1][4]
    state["publication_fault"] = True
    record_start = state["end"]
    start = record_start + A201_RECORD.size
    if len(frame) > A201_BODY_HEADER + state["capacity"] - start:
        raise RuntimeError("A201_BODY_CAPACITY_NOT_FIT_NO_CUT")
    need_end = start + len(frame)
    mapping_bytes = (need_end + state["mmap"].PAGESIZE - 1) // state["mmap"].PAGESIZE * state["mmap"].PAGESIZE
    mapping_bytes = min(mapping_bytes, A201_BODY_HEADER + state["capacity"])
    if mapping_bytes > state["mapping_bytes"]:
        old = state["mapping"]
        state["mapping"] = state["mmap"].mmap(state["fd"], mapping_bytes, access=state["mmap"].ACCESS_WRITE)
        state["mapping_bytes"] = mapping_bytes
        if old is not None:
            old.close()
    mapping = state["mapping"]
    sequence = state["sequence"] + 2
    sha = hashlib.sha256(frame).digest()
    record = A201_RECORD.pack(sequence, len(frame), sha)
    reference = A201_REFERENCE.pack(A201_REF_MAGIC, 1, state["slot"], sequence, start, len(frame), sha)
    end_word, sequence_word = struct.pack("<Q", need_end), struct.pack("<Q", sequence)
    # All allocating cursor/ledger/reference operations precede publication.
    # Cursor is monotonically reserved; no rollback and no reuse after fault.
    state["records"].append((sequence, start, len(frame), frame, reference))
    state["end"], state["sequence"] = need_end, sequence
    state["pending_original"] = frame
    mapping[start:need_end] = frame
    if bytes(mapping[start:need_end]) != frame:
        raise RuntimeError("A201_SOURCE_BODY_COPY_MISMATCH")
    mapping[record_start:start] = record
    a201_guard(state["end_ns"])
    mapping[104:112] = end_word
    mapping[96:104] = sequence_word
    # Even if this assignment fails, publication_fault remains fail-closed.
    # No allocating bookkeeping remains after the actual final commit word.
    state["publication_fault"] = False
    return reference

def a201_resolve_body(raw):
    if type(raw) is bytes and raw[:8] == A201_REF_MAGIC:
        if len(raw) != A201_REFERENCE.size:
            raise RuntimeError("A201_BODY_REFERENCE_FRAME")
        slot = A201_REFERENCE.unpack(raw)[2]
        view = A201_BODY_VIEWS.get(slot)
        if view is None:
            session, capsule_sha, hard_ns = a201_capsule_binding()
            # Coordinator sees original readonly worker views; its writable
            # own slot remains131, other source-only descriptors132..134.
            fd = 131 + slot
            readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
            state = {"readonly_fd": readonly, "slot": slot, "view": None}
            A201_BODY_STATE.setdefault("receiving", []).append(state)
            view = A201BodyView(readonly, slot, session, capsule_sha, hard_ns)
            state["view"] = view
            A201_BODY_VIEWS[slot] = view
        return view.read(raw)
    return raw

def a201_physical_bound():
    try:
        return os.pread(A201_BODY_FD, 8, 0) == A201_BODY_MAGIC
    except OSError as exc:
        if exc.errno == 9:
            return False
        raise

def a201_encode_cap(original):
    return A201_BODY_CAP if a201_physical_bound() else original

def a201_fork_plane(entry):
    """Select an EXISTING original browser-role plane before the actual fork.
    Legacy non-admitted G1 workflows keep their original descriptor scope.
    No new role, plane, process, end, or resource grant is created here.
    """
    if not a201_physical_bound():
        return None
    paths = ("archives/playwright/chrome-linux64.zip",
             "archives/playwright/chrome-headless-shell-linux64.zip",
             "archives/playwright/ffmpeg-linux.zip")
    path = entry.get("relative_path")
    if path not in paths:
        raise RuntimeError("A201_FORK_EXACT_EXISTING_BROWSER_ROLE")
    slot = paths.index(path) + 1
    fd = A201_BODY_FD + slot
    session, capsule_sha, hard_ns = a201_capsule_binding()
    a201_guard(hard_ns)
    st = os.fstat(fd)
    head = os.pread(fd, A201_BODY_HEADER, 0)
    expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, A201_BODY_CAP, 1, session, capsule_sha)
    seals = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()) or not stat.S_ISREG(st.st_mode) or st.st_size != A201_BODY_HEADER + A201_BODY_CAP or (
            fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals) or len(head) != A201_BODY_HEADER or (
            head[:96] != expected or struct.unpack_from("<QQQQ", head, 96) != (0, A201_BODY_HEADER, 0, 0)):
        raise RuntimeError("A201_FORK_EXISTING_FRESH_PLANE_BINDING")
    return fd

def a201_bind_fork_plane(plane, body, event):
    """Child-only remap; preserve original capsule plus its one Source plane."""
    global A201_BODY_STATE, A201_BODY_VIEWS
    import sys
    if plane is None:
        return (body, event)
    if len({100, A201_BODY_FD, body, event}) != 4 or plane in (100, body, event):
        raise RuntimeError("A201_FORK_DESCRIPTOR_COLLISION")
    os.dup2(plane, A201_BODY_FD, inheritable=False)
    # Fork copies are not ownership of the parent's cache or readonly views.
    A201_BODY_STATE = {}
    A201_BODY_VIEWS = {}
    if hasattr(sys, "_friday_sol073_plane_writer"):
        del sys._friday_sol073_plane_writer
    return (body, event, 100, A201_BODY_FD)

def a201_snapshot_plane(state):
    """Consume committed originals independently of delivery of a reference.
    Called only after confirmed bounded Root retirement; no exit-as-body credit.
    """
    # Bind the existing prefix list to its real owner BEFORE fallible reads.
    # A later refusal must not lose already consumed original record aliases.
    records = []
    state["snapshotted_original_frames"] = records
    state["snapshot_complete"] = False
    view = state["view"]
    a201_guard(view.end_ns)
    head = os.pread(view.fd, A201_BODY_HEADER, 0)
    if len(head) != A201_BODY_HEADER or head[:96] != view.expected:
        raise RuntimeError("A201_FINAL_PLANE_BINDING")
    sequence, end, poison, _ = struct.unpack_from("<QQQQ", head, 96)
    if sequence % 2 or poison or not A201_BODY_HEADER <= end <= A201_BODY_HEADER + view.capacity:
        raise RuntimeError("A201_FINAL_PLANE_UNCONFIRMED")
    offset = A201_BODY_HEADER
    expected_sequence = 2
    # The final sequence word commits a prefix; end can have advanced before
    # an ordinary fault in the final sequence store. Never reinterpret that
    # uncommitted suffix as a committed record or discard older originals.
    while expected_sequence <= sequence:
        a201_guard(view.end_ns)
        record = os.pread(view.fd, A201_RECORD.size, offset)
        view.reads += len(record)
        if len(record) != A201_RECORD.size:
            raise RuntimeError("A201_FINAL_RECORD_HEADER")
        seq, length, sha = A201_RECORD.unpack(record)
        start = offset + A201_RECORD.size
        if seq != expected_sequence or not 0 < length <= end - start:
            raise RuntimeError("A201_FINAL_RECORD_SEQUENCE_RANGE")
        reference = A201_REFERENCE.pack(A201_REF_MAGIC, 1, view.slot, seq, start, length, sha)
        records.append((reference, view.read(reference)))
        offset = start + length
        expected_sequence += 2
    if offset > end or sequence != expected_sequence - 2:
        raise RuntimeError("A201_FINAL_RECORD_FULL_PATHSET")
    # Strong complete ORIGINAL frame bytes and aliases remain on actual caller.
    a201_guard(view.end_ns)
    state["snapshotted_original_frames"] = tuple(records)
    if offset != end:
        state["uncommitted_published_tail"] = os.pread(view.fd, end - offset, offset)
        state["snapshot_complete"] = False
        raise RuntimeError("A201_UNCOMMITTED_TAIL_RETAINED_NO_RETIREMENT")
    state["snapshot_complete"] = True
    return state["snapshotted_original_frames"]

def a201_close_snapshotted_plane(state):
    if state.get("close_attempted"):
        return state.get("closed", False)
    if state.get("snapshot_complete") is not True:
        raise RuntimeError("A201_NO_CLOSE_BEFORE_FULL_RECORD_CUSTODY")
    a201_guard(state["end_ns"])
    state["close_attempted"] = True
    state["close_errors"] = []
    for key in ("readonly_fd", "fd"):
        fd = state.get(key)
        if fd is None:
            continue
        try:
            os.close(fd)
        except BaseException as exc:
            state["close_errors"].append((key, fd, exc))
        else:
            state[key] = None
    state["closed"] = not state["close_errors"]
    if state["closed"]:
        slot = state["slot"]
        if A201_BODY_VIEWS.get(slot) is state.get("view"):
            A201_BODY_VIEWS.pop(slot)
        state["view"] = None
        constructing = A201_BODY_STATE.get("constructing", [])
        # Retire only this exact confirmed state; original complete frames
        # remain in the actual caller receipt. Failed states stay charged.
        for index, pending in enumerate(constructing):
            if pending is state:
                del constructing[index]
                break
    # No native/error body acceptance is minted by frame retirement.
    return state["closed"]

def a201_close_unborn_plane(state):
    """No Source was born: original native FD owner confirms empty backing.
    Initialization failures retain primary/secondary originals in caller state.
    """
    if state.get("born") is not False or state.get("close_attempted"):
        raise RuntimeError("A201_UNBORN_EXACT_OWNERSHIP_REQUIRED")
    a201_guard(state["end_ns"])
    view = state.get("view")
    if view is not None:
        head = os.pread(view.fd, A201_BODY_HEADER, 0)
        if len(head) != A201_BODY_HEADER or head[:96] != view.expected or (
                struct.unpack_from("<QQQQ", head, 96) != (0, A201_BODY_HEADER, 0, 0)):
            raise RuntimeError("A201_UNBORN_NOT_CONFIRMED_EMPTY")
    state["snapshotted_original_frames"] = ()
    state["snapshot_complete"] = True
    return a201_close_snapshotted_plane(state)


def sol069_events(buffer,eof=False):
    events=[]
    while buffer:
        if buffer[0]==0:
            if len(buffer)<4:break
            size=int.from_bytes(buffer[:4],'big')
            require(8<=size<=131068,"SOL069_EVENT_TOTAL_BOUND")
            if len(buffer)<4+size:break
            event=sol069_decode(buffer[4:4+size],131068,require,
                lambda raw:inert_json(raw,131072))
            buffer=buffer[4+size:]
        else:
            at=buffer.find(b"\n")
            if at<0:break
            event=inert_json(buffer[:at],131072);buffer=buffer[at+1:]
        require(type(event) is dict,"SOL069_EVENT_TYPED")
        events.append(event);require(len(events)<=2,"EVENT_COUNT")
    require(not eof or not buffer,"SOL069_EVENT_PARTIAL_EOF_NO_BODY_CREDIT")
    return events,buffer

if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(json.dumps({"state": "REFUSED", "failure": str(exc)[:512]}))
        sys.exit(2)
