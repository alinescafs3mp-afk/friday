# ruff: noqa: E401,E501,E701,E702,E721,I001,SIM905
# fmt: off
import argparse, hashlib, json, os, re, subprocess, tempfile, unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
TIERS = ("change", "exact-release", "nightly")
KINDS = ("unit", "browser", "schema-restore", "host-tool", "candidate-artifact", "external-observation")
INVARIANT_CATALOG = frozenset("""acceptance.system-composition-and-observation archive.search-publication-and-authority audit.logging-diagnostics-and-evidence configuration.policy-version-compatibility conversation.context-reply-and-continuity delivery.transport-reminder-and-notification documents.ingestion-parsing-and-completeness durability.restart-replay-and-idempotency effects.exactly-once-publication external.live-system-observation files.attachment-generation-and-delivery graph.entity-topology-and-provenance host.process-tool-and-sandbox-containment identity.people-relations-and-tenancy media.voice-vision-and-rendering models.advice-grounding-and-capability orchestration.routing-fallback-and-turn-identity privacy.principal-and-tenant-isolation release.artifact-install-and-rollback resources.deadline-concurrency-and-quotas retrieval.recall-ranking-and-source-integrity scheduling.worker-mission-and-supervision schema.migration-backup-and-restore security.authentication-and-untrusted-input storage.transaction-and-lifecycle temporal.timeline-and-clock-consistency ui.browser-state-and-output-safety web.network-grounding-and-egress""".split())
DEFAULT_INVENTORY_PATH = Path(__file__).with_name("quality_gate_inventory.tsv")
MAX_NODEID_BYTES, MAX_BYTES, MAX_COLLECTION_BYTES, MAX_NODES = 256 << 10, 4 << 20, 64 << 20, 100_000
_MODULE_TEXT = r"tests/(?:[a-z0-9_]+/)*test_[a-z0-9_]+\.py"; _MODULE = re.compile(_MODULE_TEXT).fullmatch
_FUNCTION = re.compile(rf"({_MODULE_TEXT}(?:::(?!test_)[A-Za-z_]\w*)*::test_[A-Za-z0-9_]+)(?:\[.*\])?", re.S).fullmatch; _SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})").fullmatch; _OID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})").fullmatch; _DIGEST = re.compile(r"[0-9a-f]{64}").fullmatch; _INT = re.compile(r"(?:0|[1-9][0-9]*)").fullmatch
class InventoryError(ValueError): pass
def _require(condition: object, message: str) -> None:
    if not condition: raise InventoryError(message)
@dataclass(frozen=True, slots=True)
class FunctionRule:
    function_id: str; invariant_id: str; tier: str; execution_kind: str; max_runtime_s: int; scratch_mb: int; node_count: int; nodeids_sha256: str
    @property
    def module_path(self) -> str: return self.function_id.partition("::")[0]
@dataclass(frozen=True, slots=True)
class ClassifiedNode:
    nodeid: str; module_path: str; invariant_id: str; tier: str; execution_kind: str; max_runtime_s: int; scratch_mb: int
@dataclass(frozen=True, slots=True)
class GateInventory:
    rules: tuple[FunctionRule, ...]
    @property
    def digest(self) -> str: return hashlib.sha256(canonical_inventory_bytes(self)).hexdigest()
    @property
    def modules(self) -> tuple[str, ...]: return tuple(sorted({rule.module_path for rule in self.rules}))
    def classify(self, nodeids: tuple[str, ...]) -> tuple[ClassifiedNode, ...]:
        validate_inventory(self)
        _require(type(nodeids) is tuple and nodeids and all(isinstance(item, str) for item in nodeids), "collection must be a nonempty text tuple")
        _require(len(nodeids) == len(set(nodeids)), "collection contains duplicates")
        groups: dict[str, list[str]] = {}
        for nodeid in nodeids: groups.setdefault(function_id(nodeid), []).append(nodeid)
        rules = {rule.function_id: rule for rule in self.rules}
        _require(set(groups) == set(rules), "collected function membership differs from inventory")
        for name, exact in groups.items(): _require(len(exact) == rules[name].node_count and nodeids_sha256(tuple(exact)) == rules[name].nodeids_sha256, f"parameter set differs from inventory: {name}")
        first = {name: min(exact) for name, exact in groups.items()}
        return tuple(ClassifiedNode(nodeid, rules[function_id(nodeid)].module_path, rules[function_id(nodeid)].invariant_id, rules[function_id(nodeid)].tier, rules[function_id(nodeid)].execution_kind, rules[function_id(nodeid)].max_runtime_s, rules[function_id(nodeid)].scratch_mb if nodeid == first[function_id(nodeid)] else 0) for nodeid in nodeids)
    def validate_candidate_modules(self, root: str | os.PathLike[str], sha: str) -> tuple[str, ...]:
        tracked = candidate_test_modules(root, sha); _require(self.modules == tracked, "inventory differs from candidate test modules")
        return tracked
def _module(value: str) -> str:
    _require(_MODULE(value) is not None and str(PurePosixPath(value)) == value, f"invalid test module: {value!r}"); return value
def function_id(nodeid: str) -> str:
    try:
        encoded = nodeid.encode()
    except (AttributeError, UnicodeEncodeError) as exc:
        raise InventoryError("nodeid is not UTF-8 text") from exc
    match = _FUNCTION(nodeid)
    _require(match is not None and len(encoded) <= MAX_NODEID_BYTES and not any(unicodedata.category(character) == "Cc" for character in nodeid), f"invalid collected nodeid: {nodeid!r}")
    return match.group(1)
def nodeids_sha256(nodeids: tuple[str, ...]) -> str:
    _require(type(nodeids) is tuple and nodeids and len(nodeids) == len(set(nodeids)), "parameter set must be a nonempty unique tuple")
    digest = hashlib.sha256(b"friday-gate-function-parameters-v1\0")
    for nodeid in sorted(nodeids):
        function_id(nodeid); encoded = nodeid.encode(); digest.update(len(encoded).to_bytes(4, "big") + encoded)
    return digest.hexdigest()
def validate_inventory(value: object) -> GateInventory:
    _require(isinstance(value, GateInventory) and type(value.rules) is tuple and value.rules, "unsupported inventory shape")
    for rule in value.rules:
        _require(isinstance(rule, FunctionRule) and function_id(rule.function_id) == rule.function_id and rule.invariant_id in INVARIANT_CATALOG and rule.tier in TIERS and rule.execution_kind in KINDS and type(rule.max_runtime_s) is int and 1 <= rule.max_runtime_s <= 86_400 and type(rule.scratch_mb) is int and 0 <= rule.scratch_mb <= 65_536 and type(rule.node_count) is int and 1 <= rule.node_count <= 100_000 and _DIGEST(rule.nodeids_sha256) is not None, "invalid function rule")
        _require(not ((rule.execution_kind == "external-observation" and rule.tier != "nightly") or (rule.execution_kind == "candidate-artifact" and rule.tier != "exact-release") or (rule.execution_kind == "host-tool" and rule.tier == "change") or (rule.execution_kind == "browser" and rule.tier == "nightly")), "tier cannot provision execution kind")
    names = tuple(rule.function_id for rule in value.rules); _require(names == tuple(sorted(set(names))), "function rules are duplicate or unsorted")
    return value
def canonical_inventory_bytes(value: GateInventory) -> bytes:
    rules = validate_inventory(value).rules
    policies = sorted({(r.invariant_id, r.tier, r.execution_kind, r.max_runtime_s, r.scratch_mb) for r in rules})
    _require(len(policies) <= 4096, "too many policies")
    names = {policy: f"p{index:03x}" for index, policy in enumerate(policies)}
    rows = ["V\t1\n", *("P\t" + names[p] + "\t" + "\t".join(map(str, p)) + "\n" for p in policies)]
    current = None
    for rule in rules:
        if rule.module_path != current: current = rule.module_path; rows.append(f"M\t{current}\n")
        policy = (rule.invariant_id, rule.tier, rule.execution_kind, rule.max_runtime_s, rule.scratch_mb)
        rows.append(f"F\t{rule.function_id[len(current) + 2:]}\t{names[policy]}\t{rule.node_count}\t{rule.nodeids_sha256}\n")
    raw = "".join(rows).encode(); _require(len(raw) <= MAX_BYTES, "inventory is oversized")
    return raw
def parse_inventory_bytes(raw: bytes) -> GateInventory:
    _require(isinstance(raw, bytes) and raw and len(raw) <= MAX_BYTES and b"\0" not in raw and b"\r" not in raw and raw.endswith(b"\n"), "inventory is not canonical LF text")
    try:
        lines = raw.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise InventoryError("inventory is not UTF-8") from exc
    _require(lines and lines.pop(0) == "V\t1", "unsupported inventory version")
    policies, rules, module = {}, [], ""
    for line in lines:
        fields = line.split("\t")
        if fields[0] == "P" and len(fields) == 7 and _INT(fields[5]) and _INT(fields[6]): policies[fields[1]] = (fields[2], fields[3], fields[4], int(fields[5]), int(fields[6]))
        elif fields[0] == "M" and len(fields) == 2: module = _module(fields[1])
        elif fields[0] == "F" and len(fields) == 5 and module and fields[2] in policies and _INT(fields[3]):
            invariant, tier, kind, runtime, scratch = policies[fields[2]]; rules.append(FunctionRule(f"{module}::{fields[1]}", invariant, tier, kind, runtime, scratch, int(fields[3]), fields[4]))
        else: raise InventoryError("invalid inventory record")
    value = validate_inventory(GateInventory(tuple(rules))); _require(canonical_inventory_bytes(value) == raw, "inventory is not uniquely canonical")
    return value
def load_inventory(path: str | os.PathLike[str] = DEFAULT_INVENTORY_PATH) -> GateInventory:
    try:
        return parse_inventory_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise InventoryError(f"cannot read inventory: {path}") from exc
def _closed_object(pairs):  # noqa: ANN001, ANN202
    value = dict(pairs); _require(len(value) == len(pairs), "collection contains duplicate JSON keys"); return value
def load_collection(path: str | os.PathLike[str]) -> tuple[str, ...]:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_COLLECTION_BYTES + 1)
        _require(raw and len(raw) <= MAX_COLLECTION_BYTES, "collection manifest is empty or oversized")
        value = json.loads(raw, object_pairs_hook=_closed_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise InventoryError("collection manifest is unreadable or invalid") from exc
    _require(raw == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() and isinstance(value, dict) and set(value) == {"version", "nodeids"} and type(value["version"]) is int and value["version"] == 1 and isinstance(value["nodeids"], list), "collection manifest is not canonical version 1")
    nodeids = tuple(value["nodeids"])
    _require(nodeids and len(nodeids) <= MAX_NODES and all(isinstance(item, str) for item in nodeids) and len(nodeids) == len(set(nodeids)), "collection must contain bounded unique text nodeids")
    for nodeid in nodeids: function_id(nodeid)
    return nodeids
def refresh_inventory(value: GateInventory, nodeids: tuple[str, ...], declarations: tuple[tuple[str, str, str, str, int, int], ...] = ()) -> GateInventory:
    validate_inventory(value)
    _require(type(nodeids) is tuple and nodeids and all(isinstance(item, str) for item in nodeids) and len(nodeids) == len(set(nodeids)), "collection must be a nonempty unique text tuple")
    groups: dict[str, list[str]] = {}
    for nodeid in nodeids: groups.setdefault(function_id(nodeid), []).append(nodeid)
    existing = {rule.function_id: rule for rule in value.rules}
    _require(type(declarations) is tuple and all(type(item) is tuple and len(item) == 6 and isinstance(item[0], str) and function_id(item[0]) == item[0] for item in declarations), "declarations have an unsupported shape")
    declared = {item[0]: item for item in declarations}
    _require(len(declared) == len(declarations) and not set(declared) & set(existing) and set(declared) == set(groups) - set(existing) and not set(existing) - set(groups), "function membership drift requires exact new declarations and no removals")
    rules = []
    for name in sorted(groups):
        semantic = existing.get(name); fields = (semantic.invariant_id, semantic.tier, semantic.execution_kind, semantic.max_runtime_s, semantic.scratch_mb) if semantic else declared[name][1:]; exact = tuple(groups[name])
        rules.append(FunctionRule(name, *fields, len(exact), nodeids_sha256(exact)))
    return validate_inventory(GateInventory(tuple(rules)))
def _atomic_write(path: Path, raw: bytes) -> None:
    _require(not path.is_symlink() and path.is_file(), "inventory target must be a regular non-symlink file")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777); handle = os.fdopen(descriptor, "wb"); descriptor = -1
        with handle:
            _require(handle.write(raw) == len(raw), "short inventory write"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if descriptor >= 0: os.close(descriptor)
        temporary.unlink(missing_ok=True)
def maintain_inventory(collection: str | os.PathLike[str], target: str | os.PathLike[str] = DEFAULT_INVENTORY_PATH, *, declarations: tuple[tuple[str, str, str, str, int, int], ...] = (), write: bool = False) -> GateInventory:
    path = Path(target); updated = refresh_inventory(load_inventory(path), load_collection(collection), declarations); raw = canonical_inventory_bytes(updated)
    if write: _atomic_write(path, raw)
    else: _require(path.read_bytes() == raw, "inventory differs from authoritative collection")
    return updated
def _git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(("/usr/bin/git", "-c", f"safe.directory={root}", "-C", str(root), *args), check=False, capture_output=True, env=env, timeout=30)
    _require(not result.returncode and len(result.stdout) <= 16 << 20, "candidate Git query failed")
    return result.stdout
def candidate_test_modules(repository: str | os.PathLike[str], sha: str) -> tuple[str, ...]:
    _require(isinstance(sha, str) and _SHA(sha) is not None, "candidate SHA is not full and lowercase")
    root = Path(repository).resolve(strict=True)
    _require(_git(root, "cat-file", "-t", sha) == b"commit\n", "candidate is not a commit")
    raw = _git(root, "ls-tree", "-r", "-z", "--full-tree", sha, "--", "tests")
    _require(not raw or raw.endswith(b"\0"), "malformed candidate tree")
    paths = []
    for row in raw.removesuffix(b"\0").split(b"\0") if raw else ():
        try:
            metadata, encoded = row.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ", 2)
            path = encoded.decode()
        except (UnicodeError, ValueError) as exc:
            raise InventoryError("malformed candidate record") from exc
        pure = PurePosixPath(path)
        if len(pure.parts) < 2 or pure.parts[0] != "tests" or not pure.name.startswith("test_") or pure.suffix != ".py":
            continue
        _require(kind == b"blob" and mode in {b"100644", b"100755"} and _OID(object_id) is not None, "test module is not a regular Git blob")
        paths.append(_module(path))
    result = tuple(sorted(paths))
    _require(result and len(result) == len(set(result)), "candidate module set is empty or duplicate")
    return result
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exact full no-skip collection: --check; reviewed new functions: --write --declare. The closed gate separately validates tracked test modules.")
    parser.add_argument("--collection", required=True); parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY_PATH))
    mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument("--check", action="store_true"); mode.add_argument("--write", action="store_true")
    parser.add_argument("--declare", action="append", nargs=6, default=[], metavar=("FUNCTION", "INVARIANT", "TIER", "KIND", "RUNTIME", "SCRATCH"))
    args = parser.parse_args(argv)
    if args.check and args.declare: parser.error("--check forbids --declare")
    declarations = []
    for function, invariant, tier, kind, runtime, scratch in args.declare:
        if _INT(runtime) is None or _INT(scratch) is None: parser.error("declaration budgets must be canonical integers")
        declarations.append((function, invariant, tier, kind, int(runtime), int(scratch)))
    try:
        maintain_inventory(args.collection, args.inventory, declarations=tuple(declarations), write=args.write)
    except (InventoryError, OSError) as exc:
        parser.error(str(exc))
    return 0
if __name__ == "__main__": raise SystemExit(main())
