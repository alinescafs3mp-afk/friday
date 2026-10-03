#!/usr/bin/python3.14
"""A061 coherent browser3 correction SOURCE ONLY. Root pins/review/controls precede load.
No extraction, installation, package import, redirects, retry, or prior-tree write.
Production uses unchanged reviewed G1 worker and unchanged selected R4 helpers.
"""
import ast
import errno
import fcntl
import select
import hashlib
import json
import os
import re
import resource
import signal
import ssl
import stat
import sys
import time
import types
import urllib.parse

BASE = "/home/jericho/.jericho/runtime/subagent-lifecycle/"
STEM = "/var/tmp/friday-astra-browser-whole-control-native-a061-g1/A061"
BILL = STEM + "-BILL.json"
BILL_SHA = "488e8dc7e16d6aded4d082301f4a7e8ebf8575452a584eb7a9f70d897f7ce650"
CONTROLS = STEM + "-CONTROLS.py"
ROOT = "/var/tmp/friday-astra-browser-acquisition-20261001-a061-g1"
PARTIAL = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
G1 = BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py"
G1_SHA = "ba70d584ab4b145195283456b17d98a8ff129a8f7bb3be3595c711fe97d6c223"
R4 = BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1-R4.py"
R4_SHA = "a68f62ce16883f108992a8f237b9614d964ff0141d9002b5e714fdc2887b414c"
OWNER = "/home/jericho/.jericho/grok-takeover/ASTRA-E4-OWNER-DELEGATED-PROJECT-AUTHORITY-20261001.json"
OWNER_SHA = "a360c1f8e7b6e09b95bfea0ea13af257234a815733a796046f5b9f0167fd7a63"
INVENTORY = BASE + "ASTRA-E4-PARTIAL-ACQUISITION-CUSTODY-BROWSER-SUCCESSOR-A035-G1-PARTIAL-INVENTORY.json"
INVENTORY_SHA = "90f2076b1368b56b563d2eeba146fee8c51d76d541ac5b43864487034f1f554d"
LIMITS = {"network_gets_max": 3, "workers_global_max": 4, "header_bytes_max": 65536,
          "connect_timeout_sec": 15, "request_timeout_sec": 300, "wall_sec": 1200,
          "reserve_sec": 60, "body_bytes_max": 956301312, "disk_bytes_max": 2147483648,
          "rss_bytes_max": 268435456, "regular_files_max": 6, "directories_max": 3, "retries": 0}
EXPECTED = (
    ("chromium", "1228", "149.0.7827.55", "chrome-linux64.zip", 536870912,
     "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.55/linux64/chrome-linux64.zip"),
    ("chromium-headless-shell", "1228", "149.0.7827.55", "chrome-headless-shell-linux64.zip", 402653184,
     "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.55/linux64/chrome-headless-shell-linux64.zip"),
    ("ffmpeg", "1011", None, "ffmpeg-linux.zip", 16777216,
     "https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1011/ffmpeg-linux.zip"),
)
R4_NAMES = ("Run", "error_name", "close_fd", "stop_child", "guarded_digest", "guarded_write",
            "ordinary_failure", "unknown_record", "launch", "drain", "collect", "run_wave")
m = r = None
ACTIVE_RUN = None
HELD_BYTES = {}
HELD_ADMISSION = None
HELD_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
HELD_ROLES = {"executor": (101, STEM + "-EXECUTOR.py"), "controls": (102, CONTROLS),
    "supervisor": (103, STEM + "-SUPERVISOR.py"), "bill": (104, BILL),
    "G1": (105, G1), "R4": (106, R4), "owner": (107, OWNER),
    "inventory": (108, INVENTORY), "CA": (109, "/etc/ssl/certs/ca-certificates.crt")}


class AdmissionRefused(Exception):
    """Typed refusal before immutable helpers are loaded."""


def need(ok, reason):
    if not ok:
        raise (m.Refused if m is not None else AdmissionRefused)(reason)


def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_uid, st.st_gid, st.st_mode, st.st_nlink)


def open_nf(path):
    need(isinstance(path, str) and path.startswith("/") and
         all(p not in ("", ".", "..") for p in path.split("/")[1:]), "ABSOLUTE_PATH")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(path.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def sealed_bytes(path, sha, cap=1048576, code=False):
    if path in HELD_BYTES:
        data = HELD_BYTES[path]
        need(len(data) <= cap and hashlib.sha256(data).hexdigest() == sha, "PIN_SHA")
        return data
    fd = open_nf(path)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
             before.st_uid == before.st_gid == os.getuid() and
             stat.S_IMODE(before.st_mode) == 0o600 and before.st_size <= cap, "PIN_CUSTODY")
        data = bytearray()
        while len(data) <= cap:
            block = os.read(fd, min(65536, cap - len(data) + 1))
            if not block:
                break
            data.extend(block)
        need(len(data) == before.st_size and hashlib.sha256(data).hexdigest() == sha, "PIN_SHA")
        need(identity(before) == identity(os.fstat(fd)), "PIN_DRIFT")
        linked = open_nf(path)
        try:
            need(identity(os.fstat(linked)) == identity(before), "PIN_PATH_DRIFT")
        finally:
            os.close(linked)
        return bytes(data)
    finally:
        os.close(fd)


def install_held_capsule():
    """Consume root-owned sealed pre-interpreter approval, fixed descriptors only.
    This validates external authority; it cannot fabricate that object or prove a
    missing native launcher/runtime. Direct named-source startup fails.
    """
    global HELD_ADMISSION
    if "a061_contract" in sys.modules:
        c = sys.modules["a061_contract"]
        cap = c.ADMISSION
        need(cap is not None and cap["authority"] == "AUTHENTIC_NATIVE_START_AND_HELD_ROOT_INPUTS_VALIDATED", "A061_ACTUAL_NATIVE_START_REQUIRED")
        pending = {}
        expected = {"G1": G1_SHA, "R4": R4_SHA, "owner": OWNER_SHA, "bill": BILL_SHA,
                    "CA": "80eedd808e4cbd6fd42e125da2ea225fd1365d8e29edef8bcc45ff8bc7044ce2"}
        if cap["mode_id"] != 4: expected["inventory"] = INVENTORY_SHA
        for role, (fd, path) in HELD_ROLES.items():
            need(role not in expected or cap["pins"][role] == expected[role], "A061_IMMUTABLE_DEPENDENCY_PIN")
            pending[path] = c.held(fd, cap["pins"][role], 1048576)
        HELD_BYTES.update(pending)
        HELD_ADMISSION = c.normalized(cap, sys.modules[__name__])
        return HELD_ADMISSION
    raise AdmissionRefused("A061_ACTUAL_NATIVE_START_REQUIRED")


def adopt_held_deadline(run):
    if HELD_ADMISSION is not None and HELD_ADMISSION["mode"] != "--controls":
        run.started, run.work, run.hard = (HELD_ADMISSION[k] for k in ("started", "work", "hard"))
    return run


def load_helpers():
    """Only immutable reviewed definitions; no R4 top-level data admission effects."""
    global m, r
    g1 = sealed_bytes(G1, G1_SHA)
    r4 = sealed_bytes(R4, R4_SHA)
    m = types.ModuleType("a035_reviewed_g1")
    m.__file__ = G1
    exec(compile(g1, G1, "exec"), m.__dict__)
    tree = ast.parse(r4, R4)
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                and n.name in R4_NAMES]
    need(len(selected) == len(R4_NAMES) and {n.name for n in selected} == set(R4_NAMES),
         "EXACT_R4_HELPER_SET")
    r = types.ModuleType("a035_reviewed_r4_helpers")
    r.__dict__.update(m=m, os=os, stat=stat, sys=sys, time=time, json=json,
                     hashlib=hashlib, types=types, re=re, signal=signal,
                     select=__import__("select"), ssl=ssl,
                     UNCERTAIN_RECEIPT=b'{"event":"RUN_STATE","state":"STOP_UNCONFIRMED","terminal_completion":false}\n')
    # AST nodes are unchanged source definitions from the pinned reviewed R4.
    exec(compile(ast.Module(body=selected, type_ignores=[]), R4, "exec"), r.__dict__)


def compile_bill(plan):
    need(type(plan) is dict and plan.get("schema") == "friday.astra.e4.browser3-source-bill.v1" and
         plan.get("assignment") == "ASTRA-E4-BROWSER-NATIVE-RUNTIME-REGISTRY-CLOSURE-A061" and
         type(plan.get("generation")) is int and plan["generation"] == 1, "BILL_IDENTITY")
    need(plan["quarantine"] == ROOT and plan["limits"] == LIMITS and
         all(type(v) is int for v in plan["limits"].values()), "BILL_LIMITS")
    need(plan["helper_source_pins"] == {"G1": {"path": G1, "sha256": G1_SHA},
                                      "R4": {"path": R4, "sha256": R4_SHA}}, "HELPER_PINS")
    need(plan["owner_authority_pin"] == {"path": OWNER, "sha256": OWNER_SHA}, "OWNER_PIN")
    need(plan["original_partial_inventory_pin"] == {"path": INVENTORY, "sha256": INVENTORY_SHA},
         "PARTIAL_INVENTORY_PIN")
    need(plan["historical_local107_and_archive202_and_metadata5_no_repeat"] is True, "NO_REPEAT")
    rows = plan["browser_archives"]
    need(type(rows) is list and len(rows) == 3, "BROWSER_COUNT")
    remote = []
    for row, wanted in zip(rows, EXPECTED):
        name, revision, version, filename, cap, url = wanted
        need(type(row) is dict and row.get("name") == name and row.get("revision") == revision and
             row.get("browser_version") == version and row.get("filename") == filename and
             type(row.get("bytes_max")) is int and row["bytes_max"] == cap and
             row.get("url") == url and row.get("relative_path") == "archives/playwright/" + filename and
             row.get("size") is None and row.get("sha256") is None and
             row.get("signature_receipt") is None, "EXACT_BROWSER_ROW")
        u = urllib.parse.urlsplit(url)
        remote.append({"kind": "browser", "url": url, "host": u.hostname, "path": u.path,
                       "relative_path": row["relative_path"], "cap": cap,
                       "size": None, "sha256": None, "seconds": 300})
    paths = [e["relative_path"] for e in remote] + ["bill.json", "transport-receipts.ndjson", "inventory.json"]
    need(plan["retained_paths"] == paths and plan["retained_dirs"] == ["archives", "archives/playwright"],
         "SIX_FILE_THREE_DIRECTORY_BILL")
    need(sum(e["cap"] for e in remote) == LIMITS["body_bytes_max"], "BODY_RESERVATION")
    return remote, paths, plan["retained_dirs"]


LEXER = re.compile(r"\s+|//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|\x60(?:\\.|[^\x60\\])*\x60|[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+|[^\s]", re.S)


def tokens(source):
    need(type(source) is bytes and len(source) <= 262144, "MAPPING_SOURCE_CAP")
    text = source.decode("utf-8", "strict")
    result, end = [], 0
    for match in LEXER.finditer(text):
        need(match.start() == end, "SOURCE_LEXER_GAP")
        end = match.end()
        tok = match.group()
        if tok.isspace() or tok.startswith(("//", "/*")):
            continue
        result.append(tok)
        need(len(result) <= 100000, "SOURCE_TOKEN_CAP")
    need(end == len(text), "SOURCE_LEXER_TAIL")
    return result


def container(ts, name, opening, closing):
    found = [i for i, t in enumerate(ts) if t == name and i and ts[i - 1] == "const"]
    need(len(found) == 1, "SOURCE_CONSTANT:" + name)
    start = found[0]
    eq = next((i for i in range(start + 1, min(start + 256, len(ts))) if ts[i] == "="), None)
    need(eq is not None and ts[eq + 1] == opening, "SOURCE_CONTAINER")
    depth = 0
    for end in range(eq + 1, len(ts)):
        depth += ts[end] == opening
        depth -= ts[end] == closing
        if depth == 0:
            need(ts[end + 1:end + 2] == [";"], "SOURCE_CONTAINER_TAIL")
            return ts[eq + 2:end]
    need(False, "SOURCE_UNCLOSED")


def route_object(ts, key):
    depth, found = 0, []
    for i, t in enumerate(ts):
        if depth == 0 and t == "'" + key + "'" and ts[i + 1:i + 3] == [":", "{"]:
            end = i + 3
            while end < len(ts) and ts[end] != "}":
                need(ts[end] != "{", "SOURCE_NESTED_ROUTE")
                end += 1
            need(end < len(ts), "SOURCE_ROUTE_UNCLOSED")
            found.append(ts[i + 3:end])
        depth += t == "{"
        depth -= t == "}"
    need(len(found) == 1, "SOURCE_ROUTE_OBJECT:" + key)
    xs, mapping, i = found[0], {}, 0
    while i < len(xs):
        key = xs[i]
        need(re.fullmatch(r"'[^'\\]+'", key) and xs[i + 1:i + 2] == [":"], "PLATFORM_GRAMMAR")
        platform = key[1:-1]
        need(platform not in mapping, "PLATFORM_DUPLICATE")
        i += 2
        if xs[i:i + 2] == ["cftUrl", "("]:
            need(i + 3 < len(xs) and re.fullmatch(r"'[^'\\]+'", xs[i + 2]) and
                 xs[i + 3] == ")", "CFT_CALL_GRAMMAR")
            value = xs[i:i + 4]
            i += 4
        else:
            need(i < len(xs) and (xs[i] == "undefined" or re.fullmatch(r"'[^'\\]+'", xs[i])),
                 "ROUTE_VALUE_GRAMMAR")
            value = xs[i:i + 1]
            i += 1
        need(xs[i:i + 1] == [","], "PLATFORM_VALUE_TAIL")
        i += 1
        mapping[platform] = value
    return mapping


def mapping_check(documents, rows):
    """Bounded positive grammar, never evaluates downloaded TS/JS."""
    b = m.inert_json(documents["metadata/playwright/browsers.json"], 65536)
    need(type(b) is dict and type(b.get("browsers")) is list and len(b["browsers"]) <= 64, "BROWSER_MAP_TYPE")
    for wanted in rows:
        found = [x for x in b["browsers"] if type(x) is dict and x.get("name") == wanted["name"]]
        need(len(found) == 1 and found[0].get("revision") == wanted["revision"], "BROWSER_REVISION")
        if wanted.get("browser_version"):
            need(found[0].get("browserVersion") == wanted["browser_version"], "BROWSER_VERSION")
    c = m.inert_json(documents["metadata/playwright/cft-version.json"], 65536)
    need(type(c) is dict and c.get("version") == "149.0.7827.55" and type(c.get("downloads")) is dict,
         "CFT_VERSION_TYPE")
    for key, wanted in zip(("chrome", "chrome-headless-shell"), rows[:2]):
        choices = c["downloads"].get(key)
        need(type(choices) is list and len(choices) <= 16 and all(type(x) is dict for x in choices),
             "CFT_DOWNLOAD_TYPE")
        found = [x for x in choices if x.get("platform") == "linux64"]
        need(len(found) == 1 and found[0].get("url") == wanted["url"], "EXACT_CFT_URL")
    ts = tokens(documents["metadata/playwright/registry-index.ts"])
    starts = [i for i, t in enumerate(ts) if t == "cftUrl" and i and ts[i - 1] == "function"]
    need(len(starts) == 1, "CFT_HELPER_COUNT")
    expected = tokens(b"""function cftUrl(suffix: string): DownloadPathFunction {
return ({ browserVersion }) => { return {
path: `builds/cft/${browserVersion}/${suffix}`,
mirrors: [ 'https://cdn.playwright.dev', ], }; }; }""")
    start = starts[0] - 1
    need(ts[start:start + len(expected)] == expected and
         ts[start + len(expected):start + len(expected) + 1] == ["type"], "EXACT_CFT_HELPER_AND_TAIL")
    mirrors = container(ts, "PLAYWRIGHT_CDN_MIRRORS", "[", "]")
    need(mirrors == ["'https://cdn.playwright.dev/dbazure/download/playwright'", ",",
                     "'https://playwright.download.prss.microsoft.com/dbazure/download/playwright'", ",",
                     "'https://cdn.playwright.dev'", ","], "EXACT_MIRRORS")
    paths = container(ts, "DOWNLOAD_PATHS", "{", "}")
    platforms = {"ubuntu20.04-x64", "ubuntu22.04-x64", "ubuntu24.04-x64", "ubuntu26.04-x64",
                 "debian11-x64", "debian12-x64", "debian13-x64"}
    all_platforms = {"<unknown>", "win64", "mac10.13", "mac10.14", "mac10.15"}
    all_platforms.update("ubuntu" + version + "-" + arch
                         for version in ("18.04", "20.04", "22.04", "24.04", "26.04")
                         for arch in ("x64", "arm64"))
    all_platforms.update("debian" + version + "-" + arch
                         for version in ("11", "12", "13") for arch in ("x64", "arm64"))
    all_platforms.update("mac" + version + suffix
                         for version in ("11", "12", "13", "14", "15", "26")
                         for suffix in ("", "-arm64"))
    for name, suffix in (("chromium", "linux64/chrome-linux64.zip"),
                         ("chromium-headless-shell", "linux64/chrome-headless-shell-linux64.zip"),
                         ("ffmpeg", None)):
        mapping = route_object(paths, name)
        need(set(mapping) == all_platforms, "EXACT_ALL_PLATFORM_KEYS")
        need(platforms <= set(mapping) and mapping.get("<unknown>") == ["undefined"], "PLATFORM_SET_UNKNOWN")
        for platform in platforms:
            expected = ["cftUrl", "(", "'" + suffix + "'", ")"] if suffix else ["'builds/ffmpeg/%s/ffmpeg-linux.zip'"]
            need(mapping[platform] == expected, "EXACT_PLATFORM_ROUTE")
    return {"state": "INERT_EXACT_CFT_HELPER_PLATFORM_REVISION_AND_URL_MATCH",
            "browser_publisher_digest": "NOT_PROVEN", "learned_metadata_sha": "UNACCEPTED"}


UNCERTAIN = b'{"event":"RUN_STATE","state":"STOP_UNCONFIRMED","body_complete":false,"terminal_completion":false,"acceptance_complete":false}\n'
INCOMPLETE = b'{"event":"RUN_STATE","state":"CONTOUR_ABORTED","body_complete":false,"terminal_completion":false,"acceptance_complete":false}\n'
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def cause(exc):
    if isinstance(exc, AdmissionRefused) or (m is not None and isinstance(exc, m.Refused)):
        return str(exc)[:512]
    if isinstance(exc, OSError):
        return type(exc).__name__ + ":" + str(exc.errno)
    return type(exc).__name__


def make_run(*, clock=time.monotonic, hook=None, stop=None):
    run = r.Run(clock=clock, wall=LIMITS["wall_sec"], reserve=LIMITS["reserve_sec"], stop=stop)
    run.stages = {}
    run.trace = []
    run.deadline_trace = []
    run.terminal_reason = None
    def observe(stage, current):
        need(len(current.stages) < 128 or stage in current.stages, "TRACE_STAGE_CAP")
        current.stages[stage] = current.stages.get(stage, 0) + 1
        if len(current.trace) < 128:
            current.trace.append(stage)
        if hook:
            hook(stage, current)
        if stage in ("network_schedule", "event_drain", "closed_terminal") and len(current.deadline_trace) < 128:
            current.deadline_trace.append({"stage": stage, "elapsed": current.clock() - current.started,
                "work_remaining": current.work - current.clock(), "hard_remaining": current.hard - current.clock(),
                "children": [{"pid": c["pid"], "connected": c["connected"],
                    "elapsed": current.clock() - c["started"], "lifecycle": c["lifecycle"]}
                    for c in current.children]})
    run.hook = observe
    return run


class RetainedTree:
    """One real descriptor/path/membership consumer for production and owned fixtures.
    No body rehash: identity, no-follow reopening and exact membership only.
    Production identity admission remains in HeldPartial, independently from this consumer.
    """
    def __init__(self, root, inventory, run):
        self.root, self.run = root, run
        self.fds, self.dirs, self.ids, self.dir_ids, self.members = {}, {}, {}, {}, {}
        try:
            run.guard("retained_inventory_admission")
            need(type(inventory) is dict and inventory["root"] == root and
                 type(inventory["entries"]) is list and type(inventory["directories"]) is list and
                 type(inventory["files_count"]) is int and
                 inventory["files_count"] == len(inventory["entries"]) == 316 and
                 len(inventory["directories"]) == 13, "RETAINED_INVENTORY_SHAPE")
            keys = set()
            for row in inventory["directories"]:
                key = "" if row["path"] == "." else row["path"]
                if key:
                    m.relpath(key)
                need(key not in keys and type(row["membership"]) is list and
                     row["membership"] == sorted(set(row["membership"])) and
                     all(type(v) is str and "/" not in v and v not in ("", ".", "..")
                         for v in row["membership"]), "RETAINED_DIRECTORY_BILL")
                keys.add(key)
                fd = open_nf(root + ("/" + key if key else ""))
                self.dirs[key] = fd
                st = os.fstat(fd)
                need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == os.getuid() and
                     stat.S_IMODE(st.st_mode) == 0o700, "PARTIAL_DIRECTORY_CUSTODY")
                need(identity(st) == tuple(row["identity"]), "PARTIAL_DIRECTORY_IDENTITY")
                self.dir_ids[key], self.members[key] = tuple(row["identity"]), row["membership"]
            need("" in keys, "RETAINED_ROOT_MISSING")
            for row in inventory["entries"]:
                need(type(row) is list and len(row) == 6, "RETAINED_ENTRY_SHAPE")
                path, count, sha, ident, kind, state = row
                m.relpath(path)
                need(path not in self.fds and "/".join(path.split("/")[:-1]) in keys and
                     type(count) is int and count >= 0 and count == ident[2], "RETAINED_ENTRY_BILL")
                fd = open_nf(root + "/" + path)
                self.fds[path] = fd
                st = m.file_stat(fd, True)
                need(st.st_gid == os.getuid() and identity(st) == tuple(ident), "PARTIAL_FILE_IDENTITY")
                self.ids[path] = tuple(ident)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        self.run.guard("retained_partial_custody")
        # Membership first: a foreign/extra/missing name has its own causal refusal.
        for key, fd in self.dirs.items():
            need(sorted(os.listdir(fd)) == self.members[key], "PARTIAL_MEMBERSHIP")
        for key, fd in self.dirs.items():
            linked = open_nf(self.root + ("/" + key if key else ""))
            try:
                need(identity(os.fstat(linked))[:2] == identity(os.fstat(fd))[:2] == self.dir_ids[key][:2],
                     "PARTIAL_DIRECTORY_PATH")
            finally:
                os.close(linked)
        for path, fd in self.fds.items():
            linked = open_nf(self.root + "/" + path)
            try:
                need(identity(os.fstat(fd)) == self.ids[path] == identity(os.fstat(linked)),
                     "PARTIAL_LATE_INPUT_DRIFT")
            finally:
                os.close(linked)

        for key, fd in self.dirs.items():
            linked = open_nf(self.root + ("/" + key if key else ""))
            try:
                need(identity(os.fstat(linked)) == identity(os.fstat(fd)) == self.dir_ids[key],
                     "PARTIAL_DIRECTORY_PATH")
            finally:
                os.close(linked)

    def close(self):
        for fd in list(self.fds.values()) + list(self.dirs.values()):
            r.close_fd(fd, self.run)
        self.fds.clear()
        self.dirs.clear()


class HeldPartial(RetainedTree):
    """Fixed production all316/13 admission, no fixture selector or path override."""
    def __init__(self, plan, run):
        inv = m.inert_json(sealed_bytes(INVENTORY, INVENTORY_SHA, 524288), 524288)
        need(inv["root"] == PARTIAL and inv["files_count"] == 316 and inv["bytes"] == 420716998,
             "PARTIAL_INVENTORY_IDENTITY")
        super().__init__(PARTIAL, inv, run)
        try:
            docs = {}
            for path, pin in plan["metadata_pins"].items():
                need(pin["path"] == PARTIAL + "/" + path and path in self.fds and
                     self.ids[path] == tuple(pin["identity"]), "METADATA_PIN_PATH")
                _, _, raw = r.guarded_digest(self.fds[path], pin["bytes"], run, "metadata_preflight",
                                             pin["sha256"], pin["bytes"], collect=True)
                docs[path] = raw
            mapping_check(docs, plan["browser_archives"])
            r.guarded_digest(self.fds["bill.json"], 8388608, run, "partial_bill",
                             plan["retained_partial_pin"]["bill_sha256"])
            r.guarded_digest(self.fds["transport-receipts.ndjson"], 1048576, run, "partial_receipts",
                             plan["retained_partial_pin"]["transport_receipts_sha256"])
            self.check()
        except BaseException:
            self.close()
            raise


def disk_reservation(remote, metadata_bytes):
    need(type(metadata_bytes) is int and metadata_bytes >= 0, "RESERVATION_SIZE_TYPE")
    need(sum(e["cap"] for e in remote) + metadata_bytes + 67108864 < LIMITS["disk_bytes_max"], "RESERVATION")


def reservations(remote, raw, paths, dirs):
    need(len(remote) == 3 and len(remote) <= LIMITS["workers_global_max"], "WORKER_BOUND")
    need(sum(e["cap"] for e in remote) <= LIMITS["body_bytes_max"], "BODY_RESERVATION")
    disk_reservation(remote, len(raw))
    need(len(paths) == len(set(paths)) <= LIMITS["regular_files_max"], "OUTPUT_FILE_LIMIT")
    need(len(dirs) == len(set(dirs)) and len(dirs) + 1 <= LIMITS["directories_max"], "OUTPUT_DIRECTORY_LIMIT")


class BoundedOutput:
    """Actual G1 Output create/check/close plus fixed aggregate/count guards.
    Its recovery closes known owned descriptors after an actual Output.close error.
    """
    def __init__(self, root, paths, dirs, run):
        self.run = run
        need(len(paths) == len(set(paths)) <= LIMITS["regular_files_max"], "OUTPUT_FILE_LIMIT")
        need(len(dirs) == len(set(dirs)) and len(dirs) + 1 <= LIMITS["directories_max"], "OUTPUT_DIRECTORY_LIMIT")
        self.inner = m.Output.__new__(m.Output)
        try:
            m.Output.__init__(self.inner, root, paths, dirs)
        except BaseException:
            self.recover()
            raise
        self.fds = self.inner.fds
        run.output_owned_fds = tuple(self.fds.values())

    def recover(self):
        descriptors = set(getattr(self.inner, "fds", {}).values())
        for key in ("root", "parent"):
            fd = getattr(self.inner, key, None)
            if fd is not None:
                descriptors.add(fd)
        for fd in descriptors:
            # Avoid a second close of any fd that actual Output.close already closed.
            try:
                os.fstat(fd)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    continue
                self.run.fail("OUTPUT_RECOVERY:" + cause(exc))
                continue
            r.close_fd(fd, self.run)

    def check(self):
        self.run.guard("output_custody", True)
        self.inner.check()
        need(len(self.inner.created) <= LIMITS["regular_files_max"], "OUTPUT_FILE_LIMIT")
        need(len(self.inner.fds) <= LIMITS["directories_max"], "OUTPUT_DIRECTORY_LIMIT")
        total = 0
        for path in self.inner.created:
            parts = m.relpath(path)
            total += os.stat(parts[-1], dir_fd=self.fds["/".join(parts[:-1])],
                             follow_symlinks=False).st_size
        need(total <= LIMITS["disk_bytes_max"], "FINAL_DISK_CAP")
        return total

    def create(self, path):
        self.check()
        need(len(self.inner.created) < LIMITS["regular_files_max"], "OUTPUT_FILE_LIMIT")
        fd = self.inner.create(path)
        self.run.guard("output_created", True)
        return fd

    def close(self):
        self.run.hook("output_close", self.run)
        try:
            # The actual pinned consumer is called, including failure from external os.close.
            self.inner.close()
        except BaseException as exc:
            self.run.fail("OUTPUT_CLOSE:" + cause(exc))
            self.recover()
        self.run.guard("output_closed", True)


def ca_context(run, ca_path=None, ca_sha=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if ca_path is None and "/etc/ssl/certs/ca-certificates.crt" in HELD_BYTES:
        run.guard("ca_preflight")
        data = HELD_BYTES["/etc/ssl/certs/ca-certificates.crt"]
        need(len(data)<=1048576 and hashlib.sha256(data).hexdigest()==(m.CA_SHA if ca_sha is None else ca_sha),"INPUT_SHA")
        context.load_verify_locations(cadata=data.decode("ascii", "strict"))
        run.guard("ca_preflight")
        return context
    fd = m.absolute_open(m.CA if ca_path is None else ca_path)
    try:
        r.guarded_digest(fd, 1048576, run, "ca_preflight", m.CA_SHA if ca_sha is None else ca_sha)
        context.load_verify_locations(cafile="/proc/self/fd/" + str(fd))
    finally:
        r.close_fd(fd, run)
    return context


def prepare(plan, run):
    remote, paths, dirs = compile_bill(plan)
    owner = m.inert_json(sealed_bytes(OWNER, OWNER_SHA), 1048576)
    need(owner.get("schema") == "friday.astra.e4.owner-delegated-project-authority.v1", "OWNER_SCHEMA")
    held = HeldPartial(plan, run)
    held.check()
    return remote, paths, dirs, held


def require_absent_target(root):
    need(not os.path.lexists(root), "EXISTING_TARGET")


def preflight(plan):
    global ACTIVE_RUN
    run, held = adopt_held_deadline(make_run()), None
    ACTIVE_RUN = run
    try:
        require_absent_target(ROOT)
        _, _, _, held = prepare(plan, run)
        held.check()
    except BaseException as exc:
        run.fail(cause(exc))
    finally:
        if held:
            held.close()
        try:
            run.guard("preflight_terminal", True)
        except BaseException as exc:
            run.fail(cause(exc))
    return {"state": "REAL_RETAINED_METADATA_PATH_PREFLIGHT_PASSED" if not run.reason else run.status(),
            "reason": run.reason, "body_complete": False, "network": False,
            "target_created": False, "publisher_digest": "NOT_PROVEN", "admission": False,
            "acceptance_complete": False, "execution_install_root_gate_credit": False}


def execute_core(plan, raw, root, run, held, context, observed, worker):
    """Shared complete production effect consumer; no CLI fixture mode.
    Caller must supply a real RetainedTree, unchanged exact plan and bounded Output.
    Production execute supplies fixed HeldPartial, ROOT, real CA/resources and G1 worker.
    Controls use owned private real descriptors and replace only external transport I/O.
    """
    global ACTIVE_RUN
    ACTIVE_RUN = run
    need(type(held) in (RetainedTree, HeldPartial) and held.run is run, "REAL_RETAINED_TREE_REQUIRED")
    # Private in-process core access cannot grant writes into the original partial tree.
    parent = os.path.dirname(root)
    need(root == ROOT or (parent.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
         os.path.dirname(parent) == "/var/tmp" and root.startswith(parent + "/")), "CORE_TARGET_SCOPE")
    out = receipt = result = None
    prior = {}
    try:
        run.guard("core_admission")
        require_absent_target(root)
        remote, paths, dirs = compile_bill(plan)
        reservations(remote, raw, paths, dirs)
        held.check()
        def cancel(signum, frame):
            run.cancel = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior[sig] = signal.signal(sig, cancel)
        held.check()
        run.guard("create_fresh_browser3")
        os.umask(0o077)
        out = BoundedOutput(root, paths, dirs, run)
        fd = out.create("bill.json")
        try:
            r.guarded_write(fd, raw, run, "bill_write")
            os.fsync(fd)
            run.guard("bill_seal")
        finally:
            r.close_fd(fd, run)
        receipt = out.create("transport-receipts.ndjson")
        run.receipt = receipt
        held.check()
        if getattr(run, "native_owner", None) is not None:
            run.native_owner.wave(remote, out, context, run, receipt, worker)
        else:
            r.run_wave(remote, out, context, run, receipt, worker)
        held.check()
        if not run.reason and not run.uncertain:
            need(len(run.started_paths) == len(run.children) == len(run.results) == 3 and
                 all(c["lifecycle"] == "REAPED" for c in run.children) and
                 all(v["body_complete"] is True and v["state"] == "UNACCEPTED_UNPINNED_BODY"
                     for v in run.results.values()), "THREE_BODY_RECEIPTS")
            os.fsync(receipt)
            run.guard("receipts_fsynced", True)
            r.close_fd(receipt, run)
            receipt, run.receipt = None, None
            retained = []
            for path in paths[:-1]:
                fd = os.open(path.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW,
                             dir_fd=out.fds["/".join(path.split("/")[:-1])])
                try:
                    n, h, _ = r.guarded_digest(fd, LIMITS["disk_bytes_max"], run, "final_retained_hash", terminal=True)
                    retained.append({"path": path, "bytes": n, "sha256": h,
                                     "identity": list(m.identity(m.file_stat(fd, True)))})
                finally:
                    r.close_fd(fd, run)
            need(run.charged <= LIMITS["body_bytes_max"], "BODY_ACCOUNTING_CAP")
            manifest = {"schema": "friday.astra.e4.browser3-unaccepted-inventory.v1",
                        "state": "PREPARED_REQUIRES_EXTERNAL_ON_TIME_SEAL", "materials": run.results,
                        "retained": retained, "self_inventory": {"path": "inventory.json", "bytes": None, "sha256": None},
                        "bill_sha256": hashlib.sha256(raw).hexdigest(),
                        "charged_body_bytes": run.charged, "wire_bytes": None, "peak_workers": run.peak,
                        "resources": observed, "acceptance_complete": False,
                        "publisher_digest": "NOT_PROVEN", "future_browser_digests": "UNKNOWN_UNACCEPTED",
                        "execution_install_root_gate_credit": False}
            run.guard("inventory_serialization", True)
            data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            run.guard("inventory_serialized", True)
            need(len(data) <= 1048576 and sum(v["bytes"] for v in retained) + len(data) <= LIMITS["disk_bytes_max"],
                 "FINAL_DISK_CAP")
            fd = out.create("inventory.json")
            try:
                r.guarded_write(fd, data, run, "inventory_write", True)
                os.fsync(fd)
                run.guard("inventory_fsynced", True)
            finally:
                r.close_fd(fd, run)
            out.check()
            held.check()
            run.guard("terminal_seal", True)
            result = {"state": "BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS",
                      "body_complete": True, "inventory": root + "/inventory.json",
                      "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except BaseException as exc:
        run.fail(cause(exc))
    finally:
        # Each cleanup is isolated. Sticky uncertainty is set by unchanged R4 before
        # any secondary hook/close/allocation failure can erase it.
        for child in run.children:
            if child["lifecycle"] == "LIVE":
                if getattr(run, "native_owner", None) is not None:
                    run.native_owner.stop_child(child, run)
                else:
                    r.stop_child(child, run)
            for key in ("body", "pipe", "pidfd"):
                r.close_fd(child[key], run)
                child[key] = None
        r.close_fd(receipt, run)
        run.receipt = None
        try:
            held.close()
        except BaseException as exc:
            run.fail("HELD_CLOSE:" + cause(exc))
        if out:
            try:
                out.close()
            except BaseException as exc:
                run.fail("OUTPUT_CLOSE:" + cause(exc))
                out.recover()
        for sig, handler in prior.items():
            try:
                run.hook("signal_restore", run)
                signal.signal(sig, handler)
            except BaseException as exc:
                run.fail("SIGNAL_RESTORE:" + cause(exc))
                # Observation failures must not leave an altered owner signal handler.
                try:
                    signal.signal(sig, handler)
                except BaseException as second:
                    run.fail("SIGNAL_RESTORE_SECONDARY:" + cause(second))
        try:
            run.guard("closed_terminal", True)
        except BaseException as exc:
            run.fail(cause(exc))
    if result is None or run.reason or run.uncertain:
        result = {"state": run.status(), "body_complete": False, "inventory": None}
    result.update(reason=run.reason, errors=run.errors, uncertainty_sticky=run.uncertain,
                  uncertainty_receipt_persisted=run.uncertainty_persisted,
                  started_routes=len(run.started_paths), charged_body_bytes=run.charged,
                  peak_workers=run.peak, wire_bytes=None, materials=run.results,
                  hashes=list(run.hashes), stages=dict(run.stages),
                  deadline_trace=list(run.deadline_trace),
                  children=[{"pid": c["pid"], "lifecycle": c["lifecycle"], "connected": c["connected"]}
                            for c in run.children],
                  acceptance_complete=False, execution_install_root_gate_credit=False,
                  completed_msk=m.now(), elapsed_sec=round(run.clock() - run.started, 6),
                  resources=observed)
    return result


def acquisition_resources():
    """Real production observation/admission for the enforced64+192MiB envelope.
    The old G1 parser requires256MiB free at the leaf and is incompatible with
    this192MiB child leaf. It remains immutable and its old controls are retained;
    this actual new consumer validates the precise kernel limits instead.
    """
    def read(path, cap=65536):
        with open(path, encoding="ascii") as stream:
            text = stream.read(cap + 1)
        need(len(text) <= cap, "RESOURCE_READ_CAP")
        return text.strip()
    membership = read("/proc/self/cgroup")
    leaves = [v.split(":", 2)[2] for v in membership.splitlines() if v.startswith("0::")]
    need(len(leaves) == 1 and leaves[0] == "/friday-browser3-a061-g1", "RESOURCE_CGROUP_MEMBERSHIP")
    mounts = [v.split() for v in read("/proc/self/mountinfo", 1048576).splitlines() if " - cgroup2 " in v]
    need(len(mounts) == 1 and mounts[0][3:5] == ["/", "/sys/fs/cgroup"], "RESOURCE_CGROUP_MOUNT")
    leaf = "/sys/fs/cgroup/friday-browser3-a061-g1"
    expected = {"memory.max": "201326592", "memory.swap.max": "0", "memory.oom.group": "1",
                "pids.max": "4", "cpu.max": "max 100000"}
    observed = {key: read(leaf + "/" + key) for key in expected}
    need(observed == expected, "RESOURCE_KERNEL_ENVELOPE")
    current = read(leaf + "/memory.current")
    need(re.fullmatch("[0-9]+", current) and int(current) < 201326592, "RESOURCE_INNER_MEMORY")
    observed.update(memory_current=int(current), outer_as=67108864, aggregate_envelope=268435456,
                    actual_cgroup_membership=membership, cpu_affinity=sorted(os.sched_getaffinity(0)))
    need(bool(observed["cpu_affinity"]), "RESOURCE_CPU_AFFINITY")
    maximum, used = read("/sys/fs/cgroup/memory.max"), read("/sys/fs/cgroup/memory.current")
    need(maximum == "max" or (re.fullmatch("[0-9]+", maximum) and re.fullmatch("[0-9]+", used) and
         int(maximum) - int(used) >= 268435456), "CGROUP_ANCESTOR_MEMORY_SHORTFALL")
    observed["ancestor_limits"] = {"memory.max": maximum, "memory.current": used}
    # Real host availability and backing disk are independently required; missing
    # observations are refused or recorded as unknown, never credited as zero.
    mem = read("/proc/meminfo")
    match = re.search(r"^MemAvailable:\s+([0-9]+) kB$", mem, re.M)
    need(match is not None and int(match[1]) * 1024 >= 268435456, "MEMORY_SHORTFALL")
    available = os.statvfs("/var/tmp")
    need(available.f_bavail * available.f_frsize >= 2147483648 + 67108864, "DISK_SHORTFALL")
    observed.update(memory_available_bytes=int(match[1])*1024,
                    var_tmp_available_bytes=available.f_bavail*available.f_frsize)
    for key in ("cpu.pressure", "io.pressure", "cpuset.cpus.effective"):
        try: observed[key] = read(leaf + "/" + key)
        except FileNotFoundError: observed[key] = None
    return observed


def execute(plan, raw):
    global ACTIVE_RUN
    run, held = adopt_held_deadline(make_run()), None
    ACTIVE_RUN = run
    try:
        require_absent_target(ROOT)
        context = ca_context(run)
        # Actual unchanged resource parser and actual CA loader, no stubs in production.
        observed = acquisition_resources()
        resource.setrlimit(resource.RLIMIT_AS, (67108864, 67108864))
        _, _, _, held = prepare(plan, run)
        return execute_core(plan, raw, ROOT, run, held, context, observed, m.worker)
    except BaseException as exc:
        run.fail(cause(exc))
        if held:
            held.close()
        return {"state": run.status(), "body_complete": False, "inventory": None,
                "reason": run.reason, "acceptance_complete": False,
                "execution_install_root_gate_credit": False}


def pin_file(role, path, sha, cap):
    need(type(sha) is str and re.fullmatch("[0-9a-f]{64}", sha), "EXTERNAL_" + role + "_PIN")
    try:
        return sealed_bytes(path, sha, cap)
    except (AdmissionRefused, OSError) as exc:
        need(False, cause(exc) + ":" + role)
    except m.Refused as exc:
        need(False, cause(exc) + ":" + role)


def public_admission(argv, environ, flags, self_path):
    """Same strict public admission consumer; explicit inputs enable pure refusal checks.
    No action, target, fixture, URL or environment override flag exists.
    """
    need(flags.isolated and flags.no_site and flags.dont_write_bytecode and dict(environ) == ENV, "ISOLATED_ENV")
    if argv[1:] in ([], ["--describe"]):
        return None
    need(len(argv) == 5 and argv[1] in ("--preflight-reviewed-a061", "--execute-reviewed-a061"),
         "EXTERNAL_ROOT_PINS_REQUIRED")
    pin_file("SELF", self_path, argv[2], 1048576)
    need(argv[3] == BILL_SHA, "EXTERNAL_BILL_PIN")
    raw = pin_file("BILL", BILL, BILL_SHA, 131072)
    pin_file("CONTROLS", CONTROLS, argv[4], 1048576)
    return argv[1], raw


def terminal_bytes(result, run, dumps=None):
    """Shared terminal serialization: incomplete fixed fallback, sticky unknown first."""
    try:
        if run is not None and not run.uncertain:
            run.guard("terminal_serialization", True)
            if run.reason:
                result = dict(result, state=run.status(), body_complete=False, inventory=None,
                              reason=run.reason, acceptance_complete=False)
        data = (json.dumps if dumps is None else dumps)(result, separators=(",", ":")).encode() + b"\n"
        need(len(data) <= 1048576, "TERMINAL_OUTPUT_CAP")
        if run is not None and not run.uncertain:
            run.guard("terminal_serialized", True)
        return UNCERTAIN if run is not None and run.uncertain else data
    except BaseException as exc:
        if run is not None:
            run.terminal_reason = cause(exc)
            run.fail(cause(exc))
        return UNCERTAIN if run is not None and run.uncertain else INCOMPLETE


def emit_terminal(result, run, fd=1, dumps=None):
    """Finite nonblocking pipe write; outer supervisor must drain and own the run."""
    data = terminal_bytes(result, run, dumps)
    try:
        need(stat.S_ISFIFO(os.fstat(fd).st_mode), "TERMINAL_OUTPUT_NOT_PIPE")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        # A fixed incomplete fallback has its own one-second drain bound even if
        # the work hard deadline elapsed; it can never emit a success receipt.
        end = time.monotonic() + 1
        sent = 0
        while sent < len(data):
            if run is not None and not run.uncertain:
                # On late emission replace an unsent success with fixed incomplete.
                try:
                    run.guard("terminal_emit", True)
                except BaseException as exc:
                    run.fail(cause(exc))
                    if sent:
                        return False
                    data = INCOMPLETE
            need(time.monotonic() < end, "TERMINAL_DRAIN_TIMEOUT")
            try:
                count = os.write(fd, data[sent:])
            except BlockingIOError:
                select.select([], [fd], [], max(0, min(0.01, end - time.monotonic())))
                continue
            need(count > 0, "TERMINAL_SHORT_WRITE")
            sent += count
        return True
    except BaseException as exc:
        if run is not None:
            run.terminal_reason = cause(exc)
            run.fail("TERMINAL_SINK:" + cause(exc))
        return False


def main():
    capsule = install_held_capsule()
    admitted = public_admission(sys.argv, os.environ, types.SimpleNamespace(
        isolated=sys.flags.isolated, no_site=sys.flags.no_site, dont_write_bytecode=sys.dont_write_bytecode),
        os.path.abspath(__file__))
    if admitted is None:
        os.write(1, b'{"state":"SOURCE_ONLY_REQUIRES_INDEPENDENT_REVIEW_CONTROLS_PREFLIGHT","network":false}\n')
        return
    mode, raw = admitted
    need(capsule["mode"] == ("--preflight" if mode == "--preflight-reviewed-a061" else "--execute"),
         "HELD_CAPSULE_MODE")
    load_helpers()
    plan = m.inert_json(raw, 131072)
    compile_bill(plan)
    result = preflight(plan) if mode == "--preflight-reviewed-a061" else execute(plan, raw)
    emitted = emit_terminal(result, ACTIVE_RUN)
    return 0 if emitted and ACTIVE_RUN is not None and not ACTIVE_RUN.reason and not ACTIVE_RUN.uncertain else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        if ACTIVE_RUN is not None:
            ACTIVE_RUN.fail(cause(exc))
            emit_terminal({"state": ACTIVE_RUN.status(), "body_complete": False}, ACTIVE_RUN)
        else:
            emit_terminal({"state": "CONTOUR_ABORTED", "body_complete": False,
                           "terminal_completion": False, "acceptance_complete": False}, None)
        sys.exit(2)
