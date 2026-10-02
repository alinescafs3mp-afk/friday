"""Externally authenticated, complete descriptor-held snapshot manifests."""
import copy
import datetime
import hashlib
import re
from dataclasses import dataclass
from canonical import (ContractError, canonical_bytes, digest, exact_keys,
                       parse_canonical_object, validate_digest, validate_integer, validate_path)
from pinned_fs import PinnedRoot

SCHEMA = "friday.snapshot-manifest.v1"
MANIFEST_KEYS = ("schema", "contract", "snapshot_id", "creation_tool_sha256", "created_utc",
                 "candidate", "platform", "materials_sha256", "runtime_contract_sha256",
                 "members", "root_merkle_sha256")
ROOT_IDENTITY_KEYS = ("device", "inode", "uid", "gid", "mode", "mount_id")
CANDIDATE_KEYS = ("commit", "tree", "root_identity", "controller_path", "quality_gate_sha256",
                  "preflight_sha256", "broker_package_sha256")
PLATFORM_KEYS = ("machine", "abi", "cpython_abi", "import_suffixes", "rootfs_image_sha256",
                 "kernel_contract_sha256", "loader_contract_sha256", "snapshot_root_identity")
MEMBER_KEYS = ("path", "type", "uid", "gid", "mode", "nlink", "mount_domain")
FILE_KEYS = MEMBER_KEYS + ("size", "sha256", "executable_class")
SYMLINK_KEYS = MEMBER_KEYS + ("target", "target_sha256")
MAX_MEMBERS = 200000
MAX_FILE_BYTES = 1 << 30
MAX_TREE_BYTES = 1 << 34


class AuthenticatedManifest(dict):
    """Carries an external digest; changed bytes lose authentication."""
    def __init__(self, value, expected_sha256):
        super().__init__(value)
        self.expected_sha256 = expected_sha256


@dataclass(frozen=True)
class InstallationBinding:
    transaction_id: str
    install_identity_sha256: str
    install_grant_sha256: str
    manifest_sha256: str
    journal_sha256: str
    root_identity: object
    candidate_identity: object
    _journal_raw: bytes
    purpose: str = "installed"


def installation_binding(journal_raw, *, expected_install_identity_sha256,
                          expected_install_grant_sha256, expected_manifest_sha256,
                          snapshot_path, purpose="installed"):
    """Authenticate copied-root custody against original external grant/chain.

    Caller supplies a stable root-held immutable journal, never a staged target's
    self-authored receipt. No physical rebind is possible with inode kwargs.
    """
    from types import MappingProxyType
    from install import InstallJournal, JOURNAL_KEYS, JOURNAL_SCHEMA, _identity_projection, journal_plan_from_grant
    from install_bootstrap import parse_install_grant
    for expected in (expected_install_identity_sha256, expected_install_grant_sha256, expected_manifest_sha256):
        validate_digest(expected)
    validate_path(snapshot_path)
    if purpose not in ("staged", "installed") or type(purpose) is not str:
        raise ContractError("explicit custody receipt purpose required")
    value = parse_canonical_object(journal_raw, JOURNAL_KEYS, schema=JOURNAL_SCHEMA,
                                   max_bytes=64 << 20, max_items=10_000_000)
    projected = exact_keys(value["identity"], ("grant", "package_index_sha256", "install_grant_sha256"))
    if projected["install_grant_sha256"] != expected_install_grant_sha256:
        raise ContractError("custody receipt grant mismatch")
    grant = copy.deepcopy(projected["grant"])
    grant["authority"]["install_identity_sha256"] = "0" * 64
    grant["bootstrap_trust"]["authority_sha256"] = "0" * 64
    grant = parse_install_grant(canonical_bytes(grant), expected_install_grant_sha256)
    expected_identity = _identity_projection(grant, projected["package_index_sha256"], expected_install_grant_sha256)
    if digest(expected_identity) != expected_install_identity_sha256 or value["identity"] != expected_identity:
        raise ContractError("custody receipt immutable install identity")
    if grant["snapshot_manifest_sha256"] != expected_manifest_sha256 or snapshot_path != "usr/libexec/friday/quality-gate-toolchain-v1/" + expected_manifest_sha256:
        raise ContractError("custody receipt fixed snapshot binding")
    # Reuse the production canonical successor/phase/effect validator itself.
    plan = journal_plan_from_grant(grant, expected_identity, expected_install_identity_sha256)
    verified = InstallJournal(plan, None)._parse(journal_raw)
    if verified["phase"] not in ("payloads_staged", "snapshot_publishing", "snapshot_published", "broker_publishing", "broker_published", "authority_publishing", "authority_published", "policy_publishing", "policy_published", "installed_not_live"):
        raise ContractError("custody receipt is revoked/unprepared")
    if purpose == "installed" and verified["phase"] != "installed_not_live":
        raise ContractError("final installed receipt requires terminal installation phase")
    source = snapshot_path
    if source not in verified["objects"]:
        source = "var/lib/friday/quality-gate-v1/install/stage-" + grant["transaction_id"] + "/snapshot"
    def translate(path):
        record = verified["objects"].get(path)
        if record is None or record["type"] != "directory":
            raise ContractError("custody root missing from durable journal")
        result = dict(device=record["dev"], inode=record["ino"], uid=record["uid"], gid=record["gid"], mode=record["mode"], mount_id=record["mount"])
        validate_root_identity(result)
        return MappingProxyType(result)
    return InstallationBinding(grant["transaction_id"], expected_install_identity_sha256,
                               expected_install_grant_sha256, expected_manifest_sha256,
                               verified["journal_sha256"], translate(source), translate(source + "/candidate"), journal_raw, purpose)


def validate_root_identity(value):
    exact_keys(value, ROOT_IDENTITY_KEYS)
    for key in ROOT_IDENTITY_KEYS:
        validate_integer(value[key], maximum=0o7777 if key == "mode" else 2**63 - 1)
    if not value["inode"] or not value["mount_id"] or value["mode"] & 0o7222:
        raise ContractError("unsafe root identity")
    if value["uid"] != 0 or value["gid"] != 0:
        raise ContractError("snapshot authority must own roots")
    return value


def _text(value, maximum=256):
    if type(value) is not str or not value or len(value) > maximum or not value.isascii() or any(ord(c) < 32 for c in value):
        raise ContractError("bounded ASCII identity required")
    return value


def normalize_link(path, target):
    if type(target) is not str or not target or not target.isascii() or len(target) > 4096 or "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        raise ContractError("unsafe link target")
    parts = [] if target.startswith("/") else path.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ContractError("link escapes snapshot")
            parts.pop()
        else:
            if len(part) > 255:
                raise ContractError("overlong link target")
            parts.append(part)
    if not parts:
        raise ContractError("link resolves to unlisted root")
    return validate_path("/".join(parts))


def validate_links(members):
    mapping = {member["path"]: member for member in members}
    def resolve(path, seen):
        if path in seen or len(seen) > 32:
            raise ContractError("symlink loop/depth")
        components = path.split("/")
        for index in range(1, len(components) + 1):
            prefix = "/".join(components[:index])
            node = mapping.get(prefix)
            if node is None:
                raise ContractError("dangling link/unlisted parent")
            if node["type"] == "symlink":
                target = normalize_link(prefix, node["target"])
                suffix = "/".join(components[index:])
                return resolve(target + ("/" + suffix if suffix else ""), seen | {prefix})
            if index != len(components) and node["type"] != "directory":
                raise ContractError("link walks through nondirectory")
        return mapping[path]
    for member in members:
        components = member["path"].split("/")
        for index in range(1, len(components)):
            parent = mapping.get("/".join(components[:index]))
            if parent is None or parent["type"] != "directory":
                raise ContractError("member has non-directory/unlisted parent")
        if member["type"] == "symlink":
            resolve(member["path"], set())


def parse_snapshot_manifest(raw, expected_sha256):
    validate_digest(expected_sha256)
    value = parse_canonical_object(raw, MANIFEST_KEYS, schema=SCHEMA,
                                   expected_sha256=expected_sha256,
                                   max_bytes=64 << 20, max_items=10_000_000)
    if value["contract"] != "friday.protected-toolchain.v1":
        raise ContractError("snapshot contract")
    for key in ("snapshot_id", "creation_tool_sha256", "materials_sha256", "runtime_contract_sha256", "root_merkle_sha256"):
        validate_digest(value[key])
    if type(value["created_utc"]) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["created_utc"]) is None:
        raise ContractError("canonical UTC timestamp")
    try:
        datetime.datetime.strptime(value["created_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ContractError("invalid calendar timestamp") from exc
    candidate = exact_keys(value["candidate"], CANDIDATE_KEYS)
    for key in ("commit", "tree"):
        if type(candidate[key]) is not str or re.fullmatch(r"[0-9a-f]{40}", candidate[key]) is None:
            raise ContractError("candidate Git identity")
    validate_root_identity(candidate["root_identity"])
    validate_path(candidate["controller_path"])
    for key in ("quality_gate_sha256", "preflight_sha256", "broker_package_sha256"):
        validate_digest(candidate[key])
    platform = exact_keys(value["platform"], PLATFORM_KEYS)
    if platform["machine"] != "x86_64":
        raise ContractError("unsupported platform")
    for key in ("abi", "cpython_abi"):
        _text(platform[key])
    suffixes = platform["import_suffixes"]
    if type(suffixes) is not list or not suffixes or len(suffixes) > 16:
        raise ContractError("import suffix list")
    for suffix in suffixes:
        _text(suffix, 128)
        if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
            raise ContractError("import suffix")
    if suffixes != sorted(set(suffixes)):
        raise ContractError("import suffix order/duplicate")
    for key in ("rootfs_image_sha256", "kernel_contract_sha256", "loader_contract_sha256"):
        validate_digest(platform[key])
    validate_root_identity(platform["snapshot_root_identity"])
    members = value["members"]
    if type(members) is not list or not members or len(members) > MAX_MEMBERS:
        raise ContractError("snapshot inventory bound")
    paths = []
    total = 0
    for member in members:
        if type(member) is not dict:
            raise ContractError("member object required")
        kind = member.get("type")
        exact_keys(member, FILE_KEYS if kind == "file" else SYMLINK_KEYS if kind == "symlink" else MEMBER_KEYS)
        if kind not in ("file", "directory", "symlink"):
            raise ContractError("special/unknown member type")
        paths.append(validate_path(member["path"]))
        for key in ("uid", "gid", "mode", "nlink", "mount_domain"):
            validate_integer(member[key], maximum=0o7777 if key == "mode" else 2**63 - 1)
        if member["uid"] or member["gid"] or not member["mount_domain"] or member["mode"] & 0o7000:
            raise ContractError("member authority/special mode")
        if member["mount_domain"] != platform["snapshot_root_identity"]["mount_id"]:
            raise ContractError("unexpected manifest mount")
        if kind != "directory" and member["nlink"] != 1:
            raise ContractError("unexpected linked member")
        if kind == "directory" and not 2 <= member["nlink"] <= MAX_MEMBERS + 2:
            raise ContractError("directory link count")
        if kind != "symlink" and member["mode"] & 0o222:
            raise ContractError("writable snapshot member")
        if kind == "file":
            validate_integer(member["size"], maximum=MAX_FILE_BYTES)
            total += member["size"]
            validate_digest(member["sha256"])
            expected_class = "executable" if member["mode"] & 0o111 else "data"
            if member["executable_class"] != expected_class:
                raise ContractError("executable classification mismatch")
            basename = member["path"].rsplit("/", 1)[-1]
            if member["path"].startswith("rootfs/") and (basename.endswith((".pth", ".egg-link")) or basename in ("sitecustomize.py", "usercustomize.py") or basename.startswith("__editable__")):
                raise ContractError("unreviewed interpreter startup/editable surface")
        elif kind == "symlink":
            normalize_link(member["path"], member["target"])
            validate_digest(member["target_sha256"])
            if hashlib.sha256(member["target"].encode("ascii")).hexdigest() != member["target_sha256"]:
                raise ContractError("link target digest")
    if total > MAX_TREE_BYTES or paths != sorted(set(paths), key=lambda path: path.encode("ascii")):
        raise ContractError("inventory size/order/duplicate")
    if len({path.casefold() for path in paths}) != len(paths):
        raise ContractError("case-colliding snapshot members")
    validate_links(members)
    mapping = {item["path"]: item for item in members}
    if any(mapping.get(name, {}).get("type") != "directory" for name in ("candidate", "rootfs", "golden")):
        raise ContractError("required snapshot domains absent")
    controller = mapping.get("candidate/" + candidate["controller_path"])
    if controller is None or controller["type"] != "file" or controller["sha256"] != candidate["quality_gate_sha256"]:
        raise ContractError("controller not bound to manifested candidate")
    if digest(members) != value["root_merkle_sha256"]:
        raise ContractError("inventory Merkle commitment")
    return AuthenticatedManifest(value, expected_sha256)


def domain_identity(members, domain):
    """Canonical content/metadata identity; physical dev/inode is separate custody."""
    validate_path(domain)
    subset = [dict(item, path=item["path"][len(domain) + 1:]) for item in members if item["path"].startswith(domain + "/")]
    return digest(subset)


def validate_snapshot_requirements(manifest, requirements_raw, requirements_sha256, *, owner_approval,
                                   expected_owner_approval_sha256, expected_manifest_sha256=None):
    """Bind actual manifested subjects to an independently pinned closed contract."""
    from provenance import parse_material_requirements, APPROVAL_KEYS
    external = expected_manifest_sha256
    if external is None and type(manifest) is AuthenticatedManifest:
        external = manifest.expected_sha256
    validate_digest(external)
    manifest = parse_snapshot_manifest(canonical_bytes(dict(manifest)), external)
    validate_digest(expected_owner_approval_sha256)
    if digest(owner_approval) != expected_owner_approval_sha256:
        raise ContractError("required external owner approval digest mismatch")
    requirements = parse_material_requirements(requirements_raw, requirements_sha256)
    exact_keys(owner_approval, APPROVAL_KEYS)
    if any(manifest["platform"][key] != requirements["platform"][key] for key in requirements["platform"]):
        raise ContractError("snapshot incompatible platform/ABI/rootfs contract")
    if any(manifest["candidate"][key] != requirements["candidate_" + key] for key in ("commit", "tree")):
        raise ContractError("snapshot incompatible required candidate")
    mapping = {item["path"]: item for item in manifest["members"]}
    actual_roles = {}
    for subject in requirements["subjects"]:
        actual = mapping.get(subject["path"])
        if actual is None or actual["type"] != "file" or actual["sha256"] != subject["sha256"] or actual["executable_class"] != subject["executable_class"]:
            raise ContractError("missing/incompatible required manifested subject")
        actual_roles[subject["role"]] = subject
    declared = {"candidate_controller": manifest["candidate"]["quality_gate_sha256"],
                "preflight": manifest["candidate"]["preflight_sha256"],
                "broker_package": manifest["candidate"]["broker_package_sha256"],
                "runtime_contract": manifest["runtime_contract_sha256"],
                "kernel_contract": manifest["platform"]["kernel_contract_sha256"],
                "loader_contract": manifest["platform"]["loader_contract_sha256"]}
    if any(actual_roles[role]["sha256"] != expected for role, expected in declared.items()) or actual_roles["candidate_controller"]["path"] != "candidate/" + manifest["candidate"]["controller_path"]:
        raise ContractError("unbound declared manifest contract subject")
    for domain in ("rootfs", "golden"):
        expected = requirements[domain + "_identity_sha256"]
        if domain_identity(manifest["members"], domain) != expected or owner_approval[domain + "_identity_sha256"] != expected:
            raise ContractError("owner-approved rootfs/golden subject mismatch")
    for key in ("candidate_commit", "candidate_tree", "attempt_generation"):
        if owner_approval[key] != requirements[key]:
            raise ContractError("required owner approval subject mismatch")
    return requirements


def _observed_root(root):
    value = root.backend.fstat(root.fd)
    return dict(device=value.st_dev, inode=value.st_ino, uid=value.st_uid, gid=value.st_gid,
                mode=value.st_mode & 0o7777, mount_id=root.backend.mount(root.fd))


class HeldSnapshot:
    def __init__(self, root, manifest, expected_provenance_sha256=None, binding=None):
        self.root = root
        self.fd = root.fd
        self.manifest = copy.deepcopy(dict(manifest))
        self.manifest_sha256 = digest(self.manifest)
        self.closed = False
        self.expected_provenance_sha256 = expected_provenance_sha256
        self.binding = binding

    def revalidate(self):
        if self.closed or digest(self.manifest) != self.manifest_sha256:
            raise ContractError("held snapshot closed/manifest drift")
        self.root.check()
        root_expected = self.manifest["platform"]["snapshot_root_identity"]
        candidate_expected = self.manifest["candidate"]["root_identity"]
        expected_inventory = self.manifest["members"]
        if self.binding is not None:
            binding = installation_binding(self.binding._journal_raw,
                expected_install_identity_sha256=self.binding.install_identity_sha256,
                expected_install_grant_sha256=self.binding.install_grant_sha256,
                expected_manifest_sha256=self.manifest_sha256,
                snapshot_path="usr/libexec/friday/quality-gate-toolchain-v1/" + self.manifest_sha256,
                purpose=self.binding.purpose)
            if binding != self.binding:
                raise ContractError("custody receipt mutation")
            for original, copied in ((root_expected, binding.root_identity), (candidate_expected, binding.candidate_identity)):
                if any(original[key] != copied[key] for key in ("uid", "gid", "mode")):
                    raise ContractError("custody rebind cannot relax authority metadata")
            root_expected, candidate_expected = binding.root_identity, binding.candidate_identity
            expected_inventory = [dict(item, mount_domain=root_expected["mount_id"]) for item in expected_inventory]
        if _observed_root(self.root) != root_expected:
            raise ContractError("unexpected snapshot root")
        inventory = self.root.walk_exact(maximum_members=MAX_MEMBERS + 2, maximum_bytes=MAX_TREE_BYTES)
        metadata = {item["path"] for item in inventory} & {"manifest.v1.json", "provenance.v1.json"}
        if metadata and metadata != {"manifest.v1.json", "provenance.v1.json"}:
            raise ContractError("incomplete snapshot metadata envelope")
        if "manifest.v1.json" in metadata:
            for item in inventory:
                if item["path"] in metadata and (item["type"] != "file" or item["uid"] != 0 or item["gid"] != 0 or item["mode"] != 0o444 or item["nlink"] != 1 or item["mount_domain"] != root_expected["mount_id"]):
                    raise ContractError("unsafe snapshot metadata envelope identity")
            self.root.read_exact("manifest.v1.json", expected_sha256=self.manifest_sha256, maximum=64 << 20)
        if "provenance.v1.json" in metadata:
            if self.expected_provenance_sha256 is None:
                raise ContractError("external provenance metadata digest required")
            raw = self.root.read_exact("provenance.v1.json", expected_sha256=self.expected_provenance_sha256)
            from provenance import parse_material_provenance, material_identity_projection
            provenance = parse_material_provenance(raw, self.expected_provenance_sha256)
            if provenance["manifest_sha256"] != self.manifest_sha256 or digest(material_identity_projection(provenance)) != self.manifest["materials_sha256"] or provenance["creation_tool_sha256"] != self.manifest["creation_tool_sha256"]:
                raise ContractError("manifest/provenance identity projection mismatch")
        if [item for item in inventory if item["path"] not in metadata] != expected_inventory:
            raise ContractError("snapshot complete inventory/member mismatch")
        candidate_fd = self.root.open_beneath("candidate", directory=True)
        try:
            with self.root.handoff(candidate_fd, expected_uid=0, expected_gid=0, require_readonly=True) as candidate:
                if _observed_root(candidate) != candidate_expected:
                    raise ContractError("candidate root identity mismatch")
                candidate.read_exact(self.manifest["candidate"]["controller_path"], expected_sha256=self.manifest["candidate"]["quality_gate_sha256"], maximum=MAX_FILE_BYTES)
        finally:
            self.root.close_beneath(candidate_fd)
        return self

    def close(self):
        if not self.closed:
            self.root.close()
            self.closed = True

    def __enter__(self):
        return self.revalidate()

    def __exit__(self, *unused):
        self.close()


def verify_snapshot(root_fd, manifest, *, expected_uid=0, expected_gid=0, backend=None,
                    expected_provenance_sha256=None, expected_manifest_sha256=None,
                    installation_binding=None):
    # Reparse the supplied object; callers cannot bypass nested schema checks.
    external = expected_manifest_sha256
    if external is None and isinstance(manifest, AuthenticatedManifest):
        external = manifest.expected_sha256
    if external is None:
        raise ContractError("externally authenticated manifest required")
    approved = parse_snapshot_manifest(canonical_bytes(dict(manifest)), external)
    source = root_fd if isinstance(root_fd, PinnedRoot) else None
    if source is not None:
        source.check()
        backend = backend or source.backend
        # Clone the source capability itself, including its named origin chain.
        # Passing source.fd would silently discard that independent custody.
        root_fd = source
    if expected_provenance_sha256 is not None:
        validate_digest(expected_provenance_sha256)
    root = PinnedRoot(root_fd, expected_uid=expected_uid, expected_gid=expected_gid,
                      backend=backend, require_readonly=True)
    if installation_binding is not None and not isinstance(installation_binding, InstallationBinding):
        root.close()
        raise ContractError("authenticated typed installation binding required")
    held = HeldSnapshot(root, approved, expected_provenance_sha256, installation_binding)
    try:
        held.revalidate()
        if source is not None:
            source.check()
        return held
    except BaseException:
        held.close()
        raise
