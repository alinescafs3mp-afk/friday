"""Authentic hash-chained install journal and policy-last installation."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import posixpath
import stat
from types import SimpleNamespace
from dataclasses import dataclass

from canonical import (ContractError, canonical_bytes, digest, exact_keys,
                       parse_canonical_object, validate_digest, validate_path, validate_integer)
from install_bootstrap import (InstallCapability, RecordingBackend, INSTALL_DIRECTORY,
    JOURNAL_PATH, JOURNAL_STAGE, INSTALL_LOCK, LIVE_LOCK, NAMESPACE, PERMANENT_PREFIXES,
    TEST_TOKEN, mount_identity, _stat_identity, parse_install_grant, validate_identity,
    _issue_install_capability)
from pinned_fs import PinnedRoot, NativeBackend
from manifest import parse_snapshot_manifest, verify_snapshot
from provenance import parse_material_provenance, verify_owner_approved_materials

INSTALL_PHASES = ("install_prepared", "payloads_staged", "snapshot_publishing",
    "snapshot_published", "broker_publishing", "broker_published", "authority_publishing",
    "authority_published", "policy_publishing", "policy_published", "installed_not_live")
REMOVE_PHASES = ("remove_prepared", "policy_revoking", "policy_revoked", "live_grant_removing",
    "authority_removing", "snapshot_removing", "broker_removing", "private_stages_removing", "removed")
INSTALL_EFFECTS = ("namespace_mkdir", "install_lock", "member_create", "member_copy",
    "metadata_seal", "file_fsync", "parent_fsync", "snapshot_publish", "broker_publish",
    "authority_publish", "prerequisites_verify", "private_stage_cleanup", "policy_stage_create",
    "policy_stage_validate", "policy_publish", "installed_verify")
REMOVE_EFFECTS = ("policy_unlink", "policy_parent_fsync", "policy_absence_validate",
    "live_grant_unlink", "execution_fence", "authority_unlink", "snapshot_member_unlink",
    "snapshot_directory_remove", "broker_member_unlink", "broker_directory_remove",
    "private_member_unlink", "private_directory_remove", "execution_lock_remove", "zero_residue_validate")
JOURNAL_KEYS = frozenset({"schema", "transaction_id", "install_identity_sha256", "identity",
    "generation", "phase", "previous_journal_sha256", "journal_sha256", "retired_transaction_ids",
    "records", "objects", "completed_effects", "pending_effect"})
RECORD_KEYS = frozenset({"generation", "phase", "previous_journal_sha256", "record_sha256",
                        "objects", "completed_effects", "pending_effect", "cancelled_effect"})
INDEX_KEYS = frozenset({"schema", "members"})
INDEX_MEMBER_KEYS = frozenset({"path", "role", "mode", "size", "sha256"})
TRUST_KEYS = frozenset({"schema", "authority_id", "authority_sha256", "package_index_sha256",
    "runtime_index_sha256", "bootstrap_python_sha256", "environment", "sys_path", "allowed_fds"})
ZERO = "0" * 64
JOURNAL_SCHEMA = "friday.install-journal.v2"
BOOTSTRAP_RUNTIME_MODULES = ("canonical", "pinned_fs", "provenance", "archive", "manifest", "assemble",
    "install_bootstrap", "install", "ledger", "custody_linux", "broker_runtime", "broker_bootstrap")


def policy_bytes(grant):
    raw = (f"#{grant['caller_uid']} ALL=(root) NOPASSWD:NOSETENV: /usr/bin/python3.14 -I -B -S "
           f"/usr/libexec/friday/quality-gate-broker-v1/{grant['package_index_sha256']}/broker_bootstrap.py run-v1\n").encode("ascii")
    if hashlib.sha256(raw).hexdigest() != grant["policy_sha256"]:
        raise ContractError("policy differs from separately pinned exact command")
    return raw


def _parents(path):
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _source_identity(root, path=None):
    if path is None:
        return _stat_identity(os.fstat(root.fd), mount_identity(root.fd))
    with root.parent_lease(path) as lease:
        before = root.backend.stat(lease.name, lease.fd)
        fd = root.backend.open(lease.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, lease.fd)
        try:
            opened = root.backend.fstat(fd)
            named = root.backend.stat(lease.name, lease.fd)
            value = _stat_identity(opened, root.backend.mount(fd))
            if value != _stat_identity(before, root.backend.mount(lease.fd)) or value != _stat_identity(named, root.backend.mount(lease.fd)):
                raise ContractError("source pre/open/post identity mismatch")
            lease.check()
            return value
        finally:
            os.close(fd)


def _identity_projection(grant, package_digest, grant_digest):
    # Identity excludes phase journals and authority's self-binding field only.
    return {"grant": grant, "package_index_sha256": package_digest,
            "install_grant_sha256": grant_digest}


@dataclass
class InstallPlan:
    grant: dict
    package: dict
    source: PinnedRoot
    manifest: dict
    provenance: dict
    identity: dict
    identity_sha256: str
    expected_grant_sha256: str
    expected_package_sha256: str
    policy: bytes
    stage: str
    snapshot: str
    broker: str
    authority_path: str
    trust_path: str
    policy_path: str
    live_grant_path: str
    live_pin_path: str
    manifest_raw: bytes = b""
    provenance_raw: bytes = b""
    material_mode: str = "unproved"
    approved_materials: object = None

    @classmethod
    def from_documents(cls, package_raw, grant_raw, expected_package_sha256,
                       expected_grant_sha256, source_root, *, manifest_raw,
                       provenance_raw, artifacts, verifier, receipts,
                       fixture_authority=False, verifier_source=None, verifier_source_sha256=None,
                       requirements=None, requirements_sha256=None):
        validate_digest(expected_package_sha256)
        validate_digest(expected_grant_sha256)
        package = parse_canonical_object(package_raw, INDEX_KEYS, schema="friday.package-index.v1",
            expected_sha256=expected_package_sha256, max_bytes=64<<20, max_items=10_000_000)
        grant = parse_install_grant(grant_raw, expected_grant_sha256)
        if grant["package_index_sha256"] != expected_package_sha256:
            raise ContractError("grant/package identity mismatch")
        if not isinstance(source_root, PinnedRoot):
            raise ContractError("already-held source descriptor required")
        source_root.check()
        if _source_identity(source_root) != grant["source_root_identity"]:
            raise ContractError("source root not owner-bound")
        members = package["members"]
        if not isinstance(members, list) or not members:
            raise ContractError("empty package inventory")
        previous, index = "", {}
        for item in members:
            exact_keys(item, INDEX_MEMBER_KEYS)
            validate_path(item["path"])
            validate_digest(item["sha256"])
            if item["path"] <= previous:
                raise ContractError("package inventory is not sorted unique")
            previous = item["path"]
            if type(item["size"]) is not int or type(item["mode"]) is not int or item["size"] < 0:
                raise ContractError("invalid index metadata")
            index[item["path"]] = item
        manifest = parse_snapshot_manifest(manifest_raw, grant["snapshot_manifest_sha256"])
        provenance = parse_material_provenance(provenance_raw, grant["provenance_sha256"])
        approved = verify_owner_approved_materials(provenance,
            expected_manifest_sha256=grant["snapshot_manifest_sha256"],
            approved_authorities=grant["approved_authorities"],
            owner_approval_sha256=grant["owner_approval_sha256"],
            assembly_recipe_sha256=grant["assembly_recipe_sha256"],
            creation_tool_sha256=grant["creation_tool_sha256"], artifacts=artifacts, receipts=receipts, verifier=verifier,
            fixture_authority=fixture_authority, verifier_source=verifier_source,
            verifier_source_sha256=verifier_source_sha256, requirements=requirements,
            requirements_sha256=requirements_sha256)
        from provenance import require_approved_materials
        require_approved_materials(approved, fixture_authority=fixture_authority)
        if (manifest["materials_sha256"] != approved.materials_sha256 or
                manifest["creation_tool_sha256"] != grant["creation_tool_sha256"] or
                manifest["candidate"]["commit"] != grant["candidate_commit"] or
                manifest["candidate"]["tree"] != grant["candidate_tree"] or
                provenance["owner_approval"]["candidate_commit"] != grant["candidate_commit"] or
                provenance["owner_approval"]["candidate_tree"] != grant["candidate_tree"] or
                provenance["owner_approval"]["attempt_generation"] != grant["attempt_generation"]):
            raise ContractError("manifest/provenance/candidate external bindings disagree")
        identity = _identity_projection(grant, expected_package_sha256, expected_grant_sha256)
        identity_sha256 = digest(identity)
        authority = dict(grant["authority"])
        if authority.get("install_identity_sha256") != ZERO or authority.get("install_grant_sha256") != ZERO:
            raise ContractError("grant authority must use explicit derived-binding placeholders")
        authority["install_identity_sha256"] = identity_sha256
        authority["install_grant_sha256"] = expected_grant_sha256
        # The closed runtime parser validates every nested authority field.
        from broker_runtime import parse_installed_authority
        parse_installed_authority(canonical_bytes(authority), digest(authority))
        for key in ("candidate_commit", "candidate_tree", "attempt_generation", "authority_id", "caller_uid",
                    "package_index_sha256", "snapshot_manifest_sha256", "provenance_sha256",
                    "bootstrap_python_path", "bootstrap_python_sha256"):
            if authority[key] != grant[key]:
                raise ContractError("authority/grant mismatch: " + key)
        if (authority["sudoers_sha256"] != grant["policy_sha256"] or
                authority["candidate_controller_sha256"] != manifest["candidate"]["quality_gate_sha256"] or
                authority["broker_bundle_sha256"] != manifest["candidate"]["broker_package_sha256"]):
            raise ContractError("authority payload pins disagree with external manifest")
        trust = dict(grant["bootstrap_trust"])
        exact_keys(trust, TRUST_KEYS)
        if trust["authority_sha256"] != ZERO:
            raise ContractError("grant trust authority hash must use explicit placeholder")
        trust["authority_sha256"] = digest(authority)
        if (trust["schema"] != "friday.bootstrap-trust.v1" or
                trust["authority_id"] != grant["authority_id"] or trust["authority_sha256"] != digest(authority) or
                trust["package_index_sha256"] != expected_package_sha256 or
                trust["bootstrap_python_sha256"] != grant["bootstrap_python_sha256"] or
                not isinstance(trust["environment"], dict) or not isinstance(trust["sys_path"], list) or
                not isinstance(trust["allowed_fds"], list)):
            raise ContractError("bootstrap trust mismatch")
        stage = INSTALL_DIRECTORY + "/stage-" + grant["transaction_id"]
        snapshot = "usr/libexec/friday/quality-gate-toolchain-v1/" + grant["snapshot_manifest_sha256"]
        broker = "usr/libexec/friday/quality-gate-broker-v1/" + expected_package_sha256
        authority_path = NAMESPACE + "/authorities/" + grant["authority_id"] + ".v1.json"
        trust_path = NAMESPACE + "/bootstrap-trust/" + expected_package_sha256 + ".v1.json"
        policy_path = "etc/sudoers.d/friday-quality-gate-" + grant["authority_id"]
        live_grant_path = NAMESPACE + "/grants/" + grant["authority_id"] + ".live.v1.json"
        live_pin_path = NAMESPACE + "/grants/" + grant["authority_id"] + ".live-pin.v1.json"
        source_paths = set()
        for member in grant["members"]:
            destination = member["destination"][1:]
            if not (destination.startswith(snapshot + "/") or destination.startswith(broker + "/")):
                raise ContractError("payload destination outside exact digest roots")
            if destination == trust_path:
                raise ContractError("trust must be separately externally bound configuration")
            src = member["source"]
            if src in source_paths or src not in index:
                raise ContractError("duplicate/unindexed source")
            source_paths.add(src)
            if index[src]["sha256"] != member["sha256"] or index[src]["size"] != member["size"]:
                raise ContractError("copy not index-bound")
            if _source_identity(source_root, src) != member["source_identity"]:
                raise ContractError("source member identity mismatch")
            raw = source_root.read_exact(src, expected_sha256=member["sha256"], expected_size=member["size"])
            if member["type"] == "symlink":
                # Closed packages contain regular files only. Approved links are
                # represented by externally indexed target-string descriptors.
                target = raw.decode("ascii")
                relative = destination[len(snapshot) + 1:] if destination.startswith(snapshot + "/") else ""
                normalized = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
                if target != member["target"] or not relative or normalized.startswith("../") or normalized == "..":
                    raise ContractError("unsafe descriptor-copy symlink")
        inventory = source_root.walk_exact()
        observed_files = {m["path"]: m for m in inventory if m["type"] != "directory"}
        if set(observed_files) != set(index):
            raise ContractError("source package unindexed/missing inventory")
        for path, item in index.items():
            observed = observed_files[path]
            if observed["type"] != "file" or any(observed[key] != item[key] for key in ("mode", "size", "sha256")):
                raise ContractError("source package member drift")
        snapshot_members = {m["destination"][len("/" + snapshot) + 1:]: m for m in grant["members"]
                            if m["destination"].startswith("/" + snapshot + "/")}
        expected_members = {m["path"]: m for m in manifest["members"] if m["type"] != "directory"}
        if set(snapshot_members) != set(expected_members):
            raise ContractError("copy inventory differs from externally pinned manifest")
        expected_dirs = [{k: m[k] for k in ("path", "uid", "gid", "mode")} for m in manifest["members"] if m["type"] == "directory"]
        if grant["snapshot_directories"] != expected_dirs:
            raise ContractError("grant snapshot directories disagree with manifest")
        for entry, raw in zip(grant["snapshot_envelope"], (manifest_raw, provenance_raw)):
            if entry["size"] != len(raw) or entry["sha256"] != hashlib.sha256(raw).hexdigest():
                raise ContractError("grant envelope byte binding mismatch")
        for path, member in snapshot_members.items():
            expected = expected_members[path]
            for field in ("type", "uid", "gid", "mode", "sha256"):
                compare = "target_sha256" if field == "sha256" and member["type"] == "symlink" else field
                if member[field] != expected[compare]:
                    raise ContractError("manifested copy metadata mismatch")
        grant = dict(grant)
        grant["authority"], grant["bootstrap_trust"] = authority, trust
        return cls(grant, package, source_root, manifest, provenance, identity, identity_sha256,
                   expected_grant_sha256, expected_package_sha256, policy_bytes(grant), stage,
                   snapshot, broker, authority_path, trust_path, policy_path, live_grant_path, live_pin_path,
                   manifest_raw, provenance_raw, approved.authority_mode, approved)

    @property
    def live_effects_path(self):
        return NAMESPACE + "/grants/" + self.grant["authority_id"] + ".live-effects.v1.json"

    @property
    def retained_directory_identities(self):
        allowed = set(self.directories) - set(_parents(JOURNAL_PATH))
        retained = {item["path"]: item["identity"] for item in self.grant["retained_directories"]}
        if set(retained) - allowed:
            raise ContractError("retained directory outside exact namespace creation graph")
        return retained

    @property
    def envelope(self):
        return {self.snapshot + "/" + item["name"]: dict(item, type="file", target=None) for item in self.grant["snapshot_envelope"]}

    def staged_path(self, member):
        destination = member["destination"][1:]
        if destination.startswith(self.snapshot + "/"):
            return self.stage + "/snapshot" + destination[len(self.snapshot):]
        return self.stage + "/broker" + destination[len(self.broker):]

    @property
    def files(self):
        result = {m["destination"][1:]: m for m in self.grant["members"]}
        result.update(self.envelope)
        for path, value, mode in ((self.authority_path, self.grant["authority"], 0o400),
                (self.trust_path, self.grant["bootstrap_trust"], 0o400)):
            raw = canonical_bytes(value)
            result[path] = {"type": "file", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                           "uid": 0, "gid": 0, "mode": mode, "target": None}
        result[self.policy_path] = {"type": "file", "size": len(self.policy), "sha256": hashlib.sha256(self.policy).hexdigest(),
                                 "uid": 0, "gid": 0, "mode": 0o440, "target": None}
        return result

    @property
    def directories(self):
        required = set()
        for path in list(self.files) + [self.staged_path(m) for m in self.grant["members"]] + [JOURNAL_PATH, self.stage + "/snapshot/member", self.stage + "/broker/member",
                self.live_grant_path, self.grant["authority"]["ledger_directory"][1:] + "/member",
                PERMANENT_PREFIXES[0] + "/member", PERMANENT_PREFIXES[1] + "/member",
                self.grant["authority"]["scratch_parent"][1:] + "/member"]:
            required.update(_parents(path))
        required.discard(self.snapshot)
        required.difference_update(p for p in tuple(required) if p.startswith(self.snapshot + "/"))
        required.update(self.stage + "/snapshot/" + m["path"] for m in self.grant["snapshot_directories"])
        return tuple(sorted(required, key=lambda p: (p.count("/"), p)))

    @property
    def paths(self):
        values = set(self.directories) | set(self.files) | {JOURNAL_PATH, JOURNAL_STAGE, INSTALL_LOCK,
            LIVE_LOCK, self.live_grant_path, self.live_pin_path, self.live_effects_path, self.stage + "/policy", self.stage + "/authority",
            self.stage + "/trust"}
        values.update(self.stage + "/snapshot/" + i["name"] for i in self.grant["snapshot_envelope"])
        values.update(self.staged_path(m) for m in self.grant["members"])
        values.update(self.snapshot + "/" + m["path"] for m in self.manifest["members"] if m["type"] == "directory")
        for path in tuple(values):
            values.update(_parents(path))
        return frozenset(values)

    def capability(self, root, *, fake_root=False, operation="root-install", test_token=None,
                   revoke_grant_raw=None, expected_revoke_sha256=None):
        self._assert_request()
        live_grants = ()
        if operation == "root-install":
            from provenance import require_approved_materials
            require_approved_materials(self.approved_materials, fixture_authority=fake_root)
        approval_sha256 = self.expected_grant_sha256
        if operation == "revoke-remove":
            validate_digest(expected_revoke_sha256)
            revoke = parse_canonical_object(revoke_grant_raw,
                {"schema", "install_identity_sha256", "install_grant_sha256", "transaction_id",
                 "effect_bill", "effect_bill_sha256", "live_grants"},
                schema="friday.revoke-remove-grant.v2", expected_sha256=expected_revoke_sha256)
            if (revoke["install_identity_sha256"] != self.identity_sha256 or
                    revoke["install_grant_sha256"] != self.expected_grant_sha256 or
                    revoke["transaction_id"] != self.grant["transaction_id"]):
                raise ContractError("separate revoke authority identity mismatch")
            from install_bootstrap import EFFECT_BILL_KEYS
            bill = exact_keys(revoke["effect_bill"], EFFECT_BILL_KEYS)
            from install_bootstrap import validate_install_bill
            validate_install_bill(canonical_bytes(bill), revoke["effect_bill_sha256"], "revoke-remove")
            if (bill["schema"] != "friday.effect-bill.v1" or bill["bill_id"] != "revoke-remove" or
                    bill["version"] != 2 or bill["implies"] != [] or
                    bill["required_authority"] != "separate-exact-revoke-remove-grant" or
                    digest(bill) != revoke["effect_bill_sha256"]):
                raise ContractError("wrong separate revoke effect bill")
            if type(revoke["live_grants"]) is not list or len(revoke["live_grants"]) > 3:
                raise ContractError("exact optional live grant removals required")
            for item in revoke["live_grants"]:
                exact_keys(item, {"path", "identity", "sha256"})
                if item["path"] not in (self.live_grant_path, self.live_pin_path, self.live_effects_path):
                    raise ContractError("foreign live grant revoke target")
                validate_identity(item["identity"])
                validate_digest(item["sha256"])
            names = [item["path"] for item in revoke["live_grants"]]
            if names != sorted(set(names)):
                raise ContractError("revoke live documents must be sorted unique")
            live_grants = tuple(revoke["live_grants"])
            approval_sha256 = expected_revoke_sha256
        elif operation != "root-install":
            raise ContractError("unknown authority operation")
        return _issue_install_capability(InstallCapability(root, fake_root, self.identity_sha256,
            frozenset(install_effect_ids(self) if operation == "root-install" else removal_effect_ids(self)), operation,
            test_token=test_token, paths=self.paths, approval_sha256=approval_sha256, live_grants=live_grants,
            approved_materials=self.approved_materials if operation=="root-install" else None))

    def _assert_request(self):
        """Mutable Python plan fields cannot extend the externally pinned graph."""
        original=parse_install_grant(canonical_bytes(self.identity["grant"]),self.expected_grant_sha256)
        if digest(self.identity)!=self.identity_sha256 or original["package_index_sha256"]!=self.expected_package_sha256:
            raise ContractError("plan immutable external request drift")
        expected=journal_plan_from_grant(original,self.identity,self.identity_sha256)
        if self.grant!=expected.grant or self.policy!=expected.policy:
            raise ContractError("resolved grant/policy changed after authentication")
        self.retained_directory_identities
        for field in ("stage","snapshot","broker","authority_path","trust_path","policy_path","live_grant_path","live_pin_path"):
            if getattr(self,field)!=getattr(expected,field): raise ContractError("plan path extended beyond exact request")
        if self.source is not None:
            if digest(self.package)!=self.expected_package_sha256 or digest(self.manifest)!=original["snapshot_manifest_sha256"] or digest(self.provenance)!=original["provenance_sha256"]:
                raise ContractError("plan authenticated document changed")
            if hashlib.sha256(self.manifest_raw).hexdigest()!=original["snapshot_manifest_sha256"] or hashlib.sha256(self.provenance_raw).hexdigest()!=original["provenance_sha256"]:
                raise ContractError("plan envelope bytes changed")


def _journal_hash(value):
    projection = dict(value)
    projection.pop("journal_sha256", None)
    records = projection.pop("records")
    projection["history_head_sha256"] = records[-1]["record_sha256"]
    return digest(projection)


def journal_plan_from_grant(grant, identity, identity_sha256):
    """Reconstruct the closed graph from externally pinned grant fields only."""
    resolved = copy.deepcopy(grant)
    authority = resolved["authority"]
    authority["install_identity_sha256"] = identity_sha256
    authority["install_grant_sha256"] = identity["install_grant_sha256"]
    resolved["bootstrap_trust"]["authority_sha256"] = digest(authority)
    package = grant["package_index_sha256"]
    manifest = grant["snapshot_manifest_sha256"]
    aid = grant["authority_id"]
    stage = INSTALL_DIRECTORY + "/stage-" + grant["transaction_id"]
    snapshot = "usr/libexec/friday/quality-gate-toolchain-v1/" + manifest
    broker = "usr/libexec/friday/quality-gate-broker-v1/" + package
    directories = [dict(item, type="directory") for item in grant["snapshot_directories"]]
    return InstallPlan(resolved, {}, None, {"members": directories}, {}, identity, identity_sha256,
        identity["install_grant_sha256"], package, policy_bytes(grant), stage, snapshot, broker,
        NAMESPACE+"/authorities/"+aid+".v1.json", NAMESPACE+"/bootstrap-trust/"+package+".v1.json",
        "etc/sudoers.d/friday-quality-gate-"+aid, NAMESPACE+"/grants/"+aid+".live.v1.json",
        NAMESPACE+"/grants/"+aid+".live-pin.v1.json")


def effect_phase_map(plan):
    mapping = {}
    for effect in install_effect_ids(plan):
        kind, path = effect.split(":", 1)
        phase = "install_prepared"
        if kind == "snapshot_publish" or (kind in ("metadata_seal", "file_fsync") and (path == plan.stage+"/snapshot" or path in {plan.stage+"/snapshot/"+m["path"] for m in plan.grant["snapshot_directories"]})):
            phase = "snapshot_publishing"
        if kind == "prerequisites_verify":
            phase = "snapshot_publishing" if path == plan.stage+"/snapshot" else "policy_publishing"
        if kind == "broker_publish": phase = "broker_publishing"
        if kind == "authority_publish" or path in (plan.stage+"/authority", plan.stage+"/trust") or (kind in ("metadata_seal", "file_fsync") and (path == plan.broker or path.startswith(plan.broker+"/"))):
            phase = "authority_publishing"
        if kind.startswith("policy_") or path == plan.stage+"/policy": phase = "policy_publishing"
        if kind == "private_stage_cleanup": phase = "policy_published" if path == plan.stage else "policy_publishing"
        if kind == "installed_verify": phase = "policy_published"
        mapping[effect] = phase
    prefixes = {"policy_unlink":"policy_revoking", "policy_parent_fsync":"policy_revoking", "policy_absence_validate":"policy_revoking",
        "live_grant_unlink":"live_grant_removing", "execution_fence":"live_grant_removing", "authority_unlink":"authority_removing",
        "snapshot_member_unlink":"snapshot_removing", "snapshot_directory_remove":"snapshot_removing",
        "broker_member_unlink":"broker_removing", "broker_directory_remove":"broker_removing",
        "private_member_unlink":"private_stages_removing", "private_directory_remove":"private_stages_removing",
        "execution_lock_remove":"private_stages_removing", "zero_residue_validate":"private_stages_removing"}
    mapping.update({e:prefixes[e.split(":",1)[0]] for e in removal_effect_ids(plan)})
    return mapping


def bootstrap_effects(plan):
    return {"namespace_mkdir:"+p for p in plan.directories if p in set(_parents(JOURNAL_PATH))} | {"install_lock:"+INSTALL_LOCK}


def effect_object_contract(plan, effect):
    kind, path = effect.split(":", 1)
    add = [path] if kind in ("namespace_mkdir", "member_create", "member_copy", "metadata_seal", "policy_stage_create", "runtime_lock_create", "execution_fence") else []
    remove = [path] if kind.endswith("_unlink") or kind.endswith("_remove") or kind == "private_stage_cleanup" else []
    moved = []
    if kind == "snapshot_publish":
        moved = [plan.stage+"/snapshot", path]
    elif kind == "broker_publish":
        member = next(m for m in plan.grant["members"] if m["destination"][1:] == path)
        moved = [plan.staged_path(member), path]
    elif kind == "authority_publish":
        moved = [plan.stage + ("/authority" if path == plan.authority_path else "/trust"), path]
    elif kind == "policy_publish":
        moved = [plan.stage+"/policy", path]
    return add, remove, moved


def effect_dependencies(plan):
    ids=set(install_effect_ids(plan)) | set(removal_effect_ids(plan))
    dependencies={e:set() for e in ids}
    for effect in ids:
        kind,path=effect.split(":",1)
        if kind in ("member_copy", "metadata_seal", "file_fsync", "parent_fsync", "policy_stage_validate"):
            previous={"member_copy":"member_create", "metadata_seal":"member_copy", "file_fsync":"metadata_seal", "parent_fsync":"file_fsync", "policy_stage_validate":"file_fsync"}[kind]+":"+path
            if previous not in ids and kind=="metadata_seal":
                previous=("policy_stage_create" if path==plan.stage+"/policy" else "member_create")+":"+path
            if previous in ids: dependencies[effect].add(previous)
        if kind=="snapshot_publish":
            dependencies[effect].add("prerequisites_verify:"+plan.stage+"/snapshot")
        if kind=="broker_publish":
            member=next(m for m in plan.grant["members"] if m["destination"][1:]==path)
            dependencies[effect].add("parent_fsync:"+plan.staged_path(member))
        if kind=="authority_publish":
            stage=plan.stage+("/authority" if path==plan.authority_path else "/trust")
            dependencies[effect].add("file_fsync:"+stage)
        if kind=="policy_stage_create":
            dependencies[effect].update(("prerequisites_verify:"+plan.snapshot,"private_stage_cleanup:"+plan.stage+"/broker"))
        if kind=="policy_publish": dependencies[effect].add("policy_stage_validate:"+plan.stage+"/policy")
        if kind=="installed_verify": dependencies[effect].add("private_stage_cleanup:"+plan.stage)
        if kind=="policy_parent_fsync": dependencies[effect].add("policy_unlink:"+path)
        if kind=="policy_absence_validate": dependencies[effect].add("policy_parent_fsync:"+path)
        if kind=="execution_lock_remove": dependencies[effect].update(e for e in ids if e.startswith(("private_member_unlink:","private_directory_remove:")))
        if kind=="zero_residue_validate": dependencies[effect].add("execution_lock_remove:"+LIVE_LOCK)
    return dependencies


def _journal_bytes(value):
    # Imported identities and old history have already been authenticated. New
    # local delta records contain validated JSON primitives only, and this is
    # exactly canonical_bytes' encoding without its redundant history traversal.
    return (json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"),
                       sort_keys=True) + "\n").encode("ascii")


class InstallJournal:
    """Canonical chain includes every durable phase/effect checkpoint.

    A valid staged exact successor is promoted on recovery. Torn/foreign/stale
    stages and rollback inside the chain refuse; no journal is repaired from
    untrusted payload bytes. The externally pinned immutable identity is checked
    against every current/staged record before any payload effect.
    """
    def __init__(self, plan, backend, fault=None):
        plan._assert_request()
        self.plan, self.backend, self.fault = plan, backend, fault
        self.value = None
        self.retired = []

    def hit(self, point):
        if self.fault is not None:
            self.fault(point)

    def _parse(self, raw):
        value = parse_canonical_object(raw, JOURNAL_KEYS, schema=JOURNAL_SCHEMA,
                                       max_bytes=64<<20, max_items=10_000_000)
        if (value["transaction_id"] != self.plan.grant["transaction_id"] or
                value["install_identity_sha256"] != self.plan.identity_sha256 or value["identity"] != self.plan.identity):
            raise ContractError("journal immutable request identity mismatch")
        if value["journal_sha256"] != _journal_hash(value):
            raise ContractError("journal authentic hash mismatch")
        records = value["records"]
        if not isinstance(records, list) or not records or len(records) > 200000:
            raise ContractError("journal record chain missing/oversize")
        previous, phase, completed, objects, pending = ZERO, None, set(), {}, None
        graph = effect_phase_map(self.plan)
        dependencies = effect_dependencies(self.plan)
        allowed_paths = self.plan.paths
        if type(value["completed_effects"]) is not list or any(type(e) is not str for e in value["completed_effects"]) or value["completed_effects"] != sorted(set(value["completed_effects"])):
            raise ContractError("strict sorted unique head effect list required")
        if type(value["objects"]) is not dict:
            raise ContractError("head objects must be exact object")
        validate_integer(value["generation"], minimum=1)
        for number, record in enumerate(records, 1):
            exact_keys(record, RECORD_KEYS)
            validate_integer(record["generation"], minimum=1)
            projection = dict(record)
            projection.pop("record_sha256")
            if record["generation"] != number or record["previous_journal_sha256"] != previous or digest(projection) != record["record_sha256"]:
                raise ContractError("journal successor chain mismatch")
            current = record["phase"]
            if current not in INSTALL_PHASES + REMOVE_PHASES:
                raise ContractError("unknown phase")
            if phase is None:
                if current != "install_prepared":
                    raise ContractError("missing prepared phase")
            elif current != phase:
                phases = INSTALL_PHASES if phase in INSTALL_PHASES else REMOVE_PHASES
                allowed = (current == "remove_prepared" and phase in INSTALL_PHASES)
                if not allowed and (phases.index(phase) + 1 >= len(phases) or phases[phases.index(phase) + 1] != current):
                    raise ContractError("non-successor or revoked-to-install phase")
                if pending is not None:
                    raise ContractError("phase transition with unfinished intent")
                if not allowed:
                    required = {e for e, p in graph.items() if p == phase} - bootstrap_effects(self.plan)
                    if not required.issubset(completed):
                        raise ContractError("phase advanced before required effect obligations")
            if not isinstance(record["objects"], dict) or not isinstance(record["completed_effects"], list):
                raise ContractError("invalid checkpoint data")
            if any(type(e) is not str for e in record["completed_effects"]):
                raise ContractError("non-string effect identity")
            effect_set = set(record["completed_effects"])
            if len(effect_set) != len(record["completed_effects"]) or completed.intersection(effect_set):
                raise ContractError("effect rollback/duplication")
            if any(e not in graph or graph[e] != current for e in effect_set):
                raise ContractError("foreign or out-of-phase effect completion")
            next_pending = record["pending_effect"]
            cancelled = record["cancelled_effect"]
            if cancelled is not None and (type(cancelled) is not str or pending is None or cancelled != pending["effect"] or effect_set or record["objects"] or next_pending is not None):
                raise ContractError("invalid intent cancellation")
            if next_pending is not None:
                exact_keys(next_pending, {"effect", "add", "remove", "moved", "predecessors"})
                effect = next_pending["effect"]
                if type(effect) is not str or graph.get(effect) != current or effect in completed or pending is not None:
                    raise ContractError("foreign/duplicate/out-of-phase intent: " + str(effect) + ":" + str(current) + ":expected=" + str(graph.get(effect)) + ":generation=" + str(number))
                if not dependencies[effect].issubset(completed):
                    raise ContractError("effect intent precedes required dependencies")
                for key in ("add", "remove", "moved"):
                    paths = next_pending[key]
                    if type(paths) is not list or len(paths) > 50000 or any(type(p) is not str or p not in allowed_paths for p in paths) or len(paths) != len(set(paths)):
                        raise ContractError("intent path capability mismatch")
                if len(next_pending["moved"]) not in (0,2) or type(next_pending["predecessors"]) is not dict:
                    raise ContractError("invalid intent move/predecessors")
                if (next_pending["add"], next_pending["remove"], next_pending["moved"]) != effect_object_contract(self.plan, effect):
                    raise ContractError("intent object graph does not match exact effect")
                for path, item in next_pending["predecessors"].items():
                    if path not in allowed_paths:
                        raise ContractError("foreign intent predecessor")
                    if item is not None:
                        validate_identity(item)
                        if path in objects and objects[path] != item:
                            raise ContractError("intent predecessor does not match journal")
                        if path not in objects and next_pending["effect"] == "namespace_mkdir:" + path and item != self.plan.retained_directory_identities.get(path):
                            raise ContractError("namespace predecessor lacks exact external retained identity")
                required_predecessors = set(next_pending["add"] + next_pending["remove"] + next_pending["moved"])
                if not required_predecessors.issubset(next_pending["predecessors"]) or set(next_pending["predecessors"]) - required_predecessors - {effect.split(":",1)[1]}:
                    raise ContractError("intent predecessor inventory incomplete/foreign")
                kind, target = effect.split(":", 1)
                member = next((m for m in self.plan.grant["members"] if self.plan.staged_path(m) == target), None)
                if member is not None and member["type"] == "file":
                    if kind == "member_create" and (target in objects or next_pending["predecessors"].get(target) is not None):
                        raise ContractError("copy-member create must have absent predecessor")
                    if kind == "member_copy" and (objects.get(target) is None or
                            next_pending["predecessors"].get(target) != objects[target] or objects[target]["type"] != "file"):
                        raise ContractError("copy intent lacks exact journaled empty-member predecessor")
                if effect_set or record["objects"]:
                    raise ContractError("intent cannot assert completion/object mutation")
            elif pending is not None and effect_set != {pending["effect"]} and cancelled != pending["effect"]:
                raise ContractError("intent successor must complete its exact effect")
            elif pending is None and (effect_set or record["objects"]):
                raise ContractError("effect/object mutation without durable intent")
            for path, identity in record["objects"].items():
                validate_path(path)
                if path not in allowed_paths:
                    raise ContractError("foreign journal object path")
                if pending is None or not (path in pending["add"] + pending["remove"] + pending["moved"] or any(path.startswith(p+"/") for p in pending["moved"])):
                    raise ContractError("object delta outside exact intent")
                if identity is None:
                    if path not in objects:
                        raise ContractError("object removal without prior identity")
                    objects.pop(path)
                else:
                    validate_identity(identity)
                    if path in objects and any(objects[path][k] != identity[k] for k in ("dev", "ino", "mount", "nlink", "type")):
                        raise ContractError("journal effect adopted substituted identity")
                    kind, target = pending["effect"].split(":",1)
                    if kind == "namespace_mkdir" and pending["predecessors"].get(path) is not None and identity != pending["predecessors"][path]:
                        raise ContractError("retained namespace identity changed during reuse")
                    if kind == "metadata_seal" and path == target:
                        expected = Installer._seal_metadata(SimpleNamespace(plan=self.plan), path)
                        if any(identity[k] != expected[k] for k in ("uid", "gid", "mode")):
                            raise ContractError("journal seal metadata outside granted delta")
                    objects[path] = identity
            pending = next_pending
            completed, phase, previous = completed | effect_set, current, record["record_sha256"]
        if (value["generation"] != len(records) or value["phase"] != phase or
                value["previous_journal_sha256"] != records[-1]["previous_journal_sha256"] or
                value["objects"] != objects or set(value["completed_effects"]) != completed or value["pending_effect"] != pending):
            raise ContractError("journal head does not match authenticated chain")
        if (not isinstance(value["retired_transaction_ids"], list) or
                self.plan.grant["transaction_id"] in value["retired_transaction_ids"]):
            raise ContractError("retired transaction may not resume")
        if (any(type(item) is not str for item in value["retired_transaction_ids"]) or
                len(set(value["retired_transaction_ids"])) != len(value["retired_transaction_ids"])):
            raise ContractError("invalid retired transaction identities")
        return value

    def load(self):
        if INSTALL_LOCK not in self.backend._locks:
            raise ContractError("journal load/promotion requires held transaction lock")
        current = self.backend.exists(JOURNAL_PATH)
        staged = self.backend.exists(JOURNAL_STAGE)
        if current is None and self.plan.grant["previous_removed_journal_sha256"] != ZERO:
            raise ContractError("rollover requires its authenticated removed predecessor")
        for value in (current, staged):
            if value is not None and (value["type"] != "file" or value["uid"] != 0 or value["gid"] != 0 or
                    value["mode"] != 0o400 or value["nlink"] != 1 or value["mount"] != self.backend.mount):
                raise ContractError("journal owner/mode/link/type/mount invalid")
        if current is not None:
            raw = self.backend.read(JOURNAL_PATH, maximum=64*1024*1024)
            head = parse_canonical_object(raw, JOURNAL_KEYS, schema=JOURNAL_SCHEMA,
                                          max_bytes=64<<20, max_items=10_000_000)
            if head["install_identity_sha256"] == self.plan.identity_sha256:
                self.value = self._parse(raw)
            else:
                if staged is not None:
                    raise ContractError("distinct request cannot adopt pending journal")
                old_identity = head["identity"]
                exact_keys(old_identity, {"grant", "package_index_sha256", "install_grant_sha256"})
                old_grant = parse_install_grant(canonical_bytes(old_identity["grant"]), old_identity["install_grant_sha256"])
                if (digest(old_identity) != head["install_identity_sha256"] or
                        old_identity["package_index_sha256"] != old_grant["package_index_sha256"]):
                    raise ContractError("old journal external identity invalid")
                old_plan = journal_plan_from_grant(old_grant, old_identity, head["install_identity_sha256"])
                verified = InstallJournal(old_plan, None)._parse(raw)
                if (verified["phase"] != "removed" or
                        self.plan.grant["transaction_id"] in verified["retired_transaction_ids"] + [verified["transaction_id"]] or
                        self.plan.grant["previous_removed_journal_sha256"] != verified["journal_sha256"]):
                    raise ContractError("foreign/incomplete/retired transaction cannot roll over")
                retained = self.plan.retained_directory_identities
                reusable = {path: identity for path, identity in verified["objects"].items()
                    if path in set(self.plan.directories) - set(_parents(JOURNAL_PATH))}
                if retained != reusable or any(identity["type"] != "directory" for identity in reusable.values()):
                    raise ContractError("rollover retained directories differ from exact removed journal")
                for path, identity in retained.items():
                    if self.backend.exists(path) != identity:
                        raise ContractError("rollover retained directory identity substitution: " + path)
                self.retired = verified["retired_transaction_ids"] + [verified["transaction_id"]]
        if staged is not None:
            successor = self._parse(self.backend.read(JOURNAL_STAGE, maximum=64*1024*1024))
            if self.value is None:
                if successor["generation"] != 1:
                    raise ContractError("orphan noninitial journal stage")
            elif (successor["generation"] != self.value["generation"] + 1 or
                    successor["records"][:-1] != self.value["records"]):
                raise ContractError("stage is not exact successor")
            self.backend.journal_replace(JOURNAL_STAGE, JOURNAL_PATH, current)
            self.value = successor
        return self.value

    def commit(self, phase, *, objects=None, completed=None, phase_boundary=True, pending_effect=None, boundary=None, cancelled_effect=None):
        if self.backend.exists(JOURNAL_STAGE) is not None:
            raise ContractError("preexisting journal stage")
        previous = self.value
        records = [] if previous is None else list(previous["records"])
        current_objects = dict(objects if objects is not None else {} if previous is None else previous["objects"])
        current_completed = set(completed if completed is not None else [] if previous is None else previous["completed_effects"])
        old_objects = {} if previous is None else previous["objects"]
        old_completed = set() if previous is None else set(previous["completed_effects"])
        changes = {path: value for path, value in current_objects.items() if old_objects.get(path) != value}
        changes.update({path: None for path in old_objects if path not in current_objects})
        record = {"generation": len(records) + 1, "phase": phase,
            "previous_journal_sha256": ZERO if not records else records[-1]["record_sha256"],
            "objects": changes, "completed_effects": sorted(current_completed - old_completed), "pending_effect": pending_effect,
            "cancelled_effect": cancelled_effect}
        record["record_sha256"] = digest(record)
        records.append(record)
        value = {"schema": JOURNAL_SCHEMA, "transaction_id": self.plan.grant["transaction_id"],
            "install_identity_sha256": self.plan.identity_sha256, "identity": self.plan.identity,
            "generation": record["generation"], "phase": phase,
            "previous_journal_sha256": record["previous_journal_sha256"],
            "retired_transaction_ids": self.retired if previous is None else previous["retired_transaction_ids"],
            "records": records, "objects": current_objects, "completed_effects": sorted(current_completed), "pending_effect": pending_effect}
        value["journal_sha256"] = _journal_hash(value)
        # The loaded predecessor is already authenticated. Validate its exact
        # successor locally; replaying/scanning the entire growing chain at every
        # mutation adds quadratic work without a new trust boundary.
        if previous is None:
            if phase != "install_prepared":
                raise ContractError("journal must begin prepared")
        elif phase != previous["phase"]:
            graph = INSTALL_PHASES if previous["phase"] in INSTALL_PHASES else REMOVE_PHASES
            allowed = phase == "remove_prepared" and previous["phase"] in INSTALL_PHASES
            position = graph.index(previous["phase"])
            if not allowed and (position + 1 >= len(graph) or graph[position + 1] != phase):
                raise ContractError("journal commit not exact successor")
        if not old_completed.issubset(current_completed):
            raise ContractError("commit would roll back completed effects")
        for path, identity in current_objects.items():
            validate_path(path)
            validate_identity(identity)
        prefix = "journal_stage:" + phase if phase_boundary else boundary or "effect_journal:" + str(record["generation"])
        self.hit("pre:" + prefix)
        self.backend.create(JOURNAL_STAGE, _journal_bytes(value))
        self.backend.seal(JOURNAL_STAGE, 0, 0, 0o400)
        self.backend.fsync(JOURNAL_STAGE)
        self.hit("post:" + prefix)
        if phase_boundary:
            self.hit("pre:phase:" + phase)
        else:
            self.hit("pre:publish:" + prefix)
        self.backend.journal_replace(JOURNAL_STAGE, JOURNAL_PATH, self.backend.exists(JOURNAL_PATH))
        self.value = value
        if phase_boundary:
            self.hit("post:phase:" + phase)
        else:
            self.hit("post:publish:" + prefix)
        return value

    def revalidate_objects(self):
        if self.value is None:
            return
        for path, expected in self.value["objects"].items():
            if self.backend.exists(path) != expected:
                raise ContractError("journaled object substituted or missing: " + path)


def install_effect_ids(plan):
    result = ["namespace_mkdir:" + p for p in plan.directories]
    result.append("install_lock:" + INSTALL_LOCK)
    result += ["runtime_lock_create:" + LIVE_LOCK, "metadata_seal:" + plan.grant["authority"]["scratch_parent"][1:]]
    for item in plan.grant["snapshot_envelope"]:
        path = plan.stage+"/snapshot/"+item["name"]
        result += [label+":"+path for label in ("member_create", "metadata_seal", "file_fsync")]
    for member in plan.grant["members"]:
        stage = plan.staged_path(member)
        result.extend(prefix + ":" + stage for prefix in ("member_create", "member_copy", "metadata_seal", "file_fsync", "parent_fsync"))
    for path in [plan.stage + "/snapshot"] + [plan.stage + "/snapshot/" + m["path"] for m in plan.manifest["members"] if m["type"] == "directory"]:
        result += ["metadata_seal:" + path, "file_fsync:" + path]
    result += ["prerequisites_verify:" + plan.stage + "/snapshot", "snapshot_publish:" + plan.snapshot]
    for path in [p for p in plan.directories if p == plan.broker or p.startswith(plan.broker + "/")]:
        result += ["metadata_seal:" + path, "file_fsync:" + path]
    for member in plan.grant["members"]:
        target = member["destination"][1:]
        if target.startswith(plan.broker + "/"):
            result.append("broker_publish:" + target)
    for label, path in (("authority", plan.authority_path), ("trust", plan.trust_path)):
        result += ["member_create:" + plan.stage + "/" + label,
                   "metadata_seal:" + plan.stage + "/" + label,
                   "file_fsync:" + plan.stage + "/" + label, "authority_publish:" + path]
    result += ["prerequisites_verify:" + plan.snapshot, "private_stage_cleanup:" + plan.stage + "/broker",
        "policy_stage_create:" + plan.stage + "/policy", "metadata_seal:" + plan.stage + "/policy",
        "file_fsync:" + plan.stage + "/policy", "policy_stage_validate:" + plan.stage + "/policy",
        "policy_publish:" + plan.policy_path, "private_stage_cleanup:" + plan.stage,
        "installed_verify:" + plan.snapshot]
    return tuple(result)


def removal_effect_ids(plan):
    result = ["policy_unlink:" + plan.policy_path, "policy_parent_fsync:" + plan.policy_path,
        "policy_absence_validate:" + plan.policy_path, "live_grant_unlink:" + plan.live_grant_path,
        "live_grant_unlink:" + plan.live_pin_path, "live_grant_unlink:" + plan.live_effects_path, "execution_fence:" + LIVE_LOCK,
        "authority_unlink:" + plan.authority_path, "authority_unlink:" + plan.trust_path]
    for target in sorted(plan.files):
        if target.startswith(plan.snapshot + "/"):
            result.append("snapshot_member_unlink:" + target)
        elif target.startswith(plan.broker + "/"):
            result.append("broker_member_unlink:" + target)
    subdirs = set()
    subdirs.update(("snapshot_directory_remove", plan.snapshot + "/" + m["path"])
                   for m in plan.manifest["members"] if m["type"] == "directory")
    for target in plan.files:
        for parent in _parents(target):
            if parent == plan.snapshot or parent.startswith(plan.snapshot + "/"):
                subdirs.add(("snapshot_directory_remove", parent))
            elif parent == plan.broker or parent.startswith(plan.broker + "/"):
                subdirs.add(("broker_directory_remove", parent))
    result += [label + ":" + path for label, path in sorted(subdirs, key=lambda x: (-x[1].count("/"), x[1]))]
    staged = set(plan.staged_path(m) for m in plan.grant["members"]) | {plan.stage + "/authority", plan.stage + "/trust", plan.stage + "/policy"}
    staged.update(plan.stage+"/snapshot/"+item["name"] for item in plan.grant["snapshot_envelope"])
    result += ["private_member_unlink:" + p for p in sorted(staged)]
    dirs = {plan.stage}
    dirs.update(plan.stage + "/snapshot/" + m["path"] for m in plan.manifest["members"] if m["type"] == "directory")
    for path in staged:
        dirs.update(p for p in _parents(path) if p.startswith(plan.stage + "/"))
    result += ["private_directory_remove:" + p for p in sorted(dirs, key=lambda p: (-p.count("/"), p))]
    result += ["execution_lock_remove:" + LIVE_LOCK, "zero_residue_validate:" + NAMESPACE]
    return tuple(result)


def phase_faults(phases):
    return tuple(point + phase for phase in phases for point in
                 ("pre:journal_stage:", "post:journal_stage:", "pre:phase:", "post:phase:"))


def install_fault_points(plan):
    return phase_faults(INSTALL_PHASES) + effect_faults(install_effect_ids(plan), omitted=bootstrap_effects(plan))


def removal_fault_points(plan):
    return phase_faults(REMOVE_PHASES) + effect_faults(removal_effect_ids(plan))


def effect_faults(effects, *, omitted=()):
    result = []
    for effect in effects:
        result += ["pre:effect:"+effect, "post:effect:"+effect]
        if effect not in omitted:
            result += ["applied:effect:"+effect]
            for journal in ("intent_journal:", "effect_journal:"):
                result += [side+":"+journal+effect for side in ("pre", "post")]
                result += [side+":publish:"+journal+effect for side in ("pre", "post")]
    return tuple(result)


class _MetadataObservation(NativeBackend):
    """Adapt modeled uid/mode observations to the actual manifest verifier."""
    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    def _rewrite(self, info, path):
        relative = os.path.relpath(path, self.backend.capability.root)
        fields = self.backend._metadata.get(relative, {})
        if not fields:
            return info
        values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid",
                  "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
        if fields.get("type") == "symlink":
            values["st_mode"] = stat.S_IFLNK | 0o777
        if "mode" in fields:
            values["st_mode"] = stat.S_IFMT(values["st_mode"]) | fields["mode"]
        if "uid" in fields:
            values["st_uid"] = fields["uid"]
        if "gid" in fields:
            values["st_gid"] = fields["gid"]
        for key, name in (("ino", "st_ino"), ("dev", "st_dev"), ("nlink", "st_nlink")):
            if key in fields and not (key == "nlink" and fields[key] == 0):
                values[name] = fields[key]
        return SimpleNamespace(**values)

    def fstat(self, fd):
        return self._rewrite(os.fstat(fd), os.readlink("/proc/self/fd/" + str(fd)))

    def stat(self, name, fd):
        return self._rewrite(os.stat(name, dir_fd=fd, follow_symlinks=False),
                             os.path.join(os.readlink("/proc/self/fd/" + str(fd)), name))

    def readlink(self, name, fd):
        path = os.path.join(os.readlink("/proc/self/fd/" + str(fd)), name)
        relative = os.path.relpath(path, self.backend.capability.root)
        target = self.backend._metadata.get(relative, {}).get("target")
        return target if target is not None else super().readlink(name, fd)

    def mount(self, fd):
        relative = os.path.relpath(os.readlink("/proc/self/fd/" + str(fd)), self.backend.capability.root)
        return self.backend._metadata.get(relative, {}).get("mount", self.backend.mount)

    def xattrs(self, fd):
        relative = os.path.relpath(os.readlink("/proc/self/fd/" + str(fd)), self.backend.capability.root)
        return self.backend._metadata.get(relative, {}).get("xattrs", super().xattrs(fd))


class Installer:
    def __init__(self, plan, backend, fault=None):
        if backend.capability.identity_sha256 != plan.identity_sha256:
            raise ContractError("backend capability not bound to request")
        self.plan, self.backend, self.fault = plan, backend, fault
        self.journal = InstallJournal(plan, backend, fault)

    def _effect(self, effect, operation, *, add=(), remove=(), moved=None):
        if effect not in self.backend.capability.effects:
            raise ContractError("effect absent from capability")
        if self.journal.value is not None and effect in self.journal.value["completed_effects"]:
            return
        self.journal.hit("pre:effect:"+effect)
        self.plan._assert_request()
        from install_bootstrap import require_install_capability
        require_install_capability(self.backend.capability)
        if self.journal.value is None:
            operation()
            self.journal.hit("post:effect:"+effect)
            return
        pending = self.journal.value["pending_effect"]
        if pending is not None:
            if pending["effect"] != effect:
                raise ContractError("unfinished different effect intent")
            if self._resolve_pending():
                return
        paths = set(add+remove+(moved or ()))
        _, target = effect.split(":",1)
        if target in self.journal.value["objects"]:
            paths.add(target)
        predecessors = {}
        for path in sorted(paths):
            observed = self.backend.exists(path)
            expected = self.journal.value["objects"].get(path)
            if expected is not None and observed != expected:
                raise ContractError("pre-effect journal identity substitution: "+path)
            if expected is None and observed is not None and path not in (LIVE_LOCK,):
                retained = self.plan.retained_directory_identities.get(path)
                if effect == "namespace_mkdir:" + path and retained is not None:
                    if observed != retained:
                        raise ContractError("external retained directory identity substitution: " + path)
                elif path not in self.backend.live_grant_identities:
                    raise ContractError("ungranted pre-effect artifact: "+path)
                elif observed != self.backend.live_grant_identities[path]:
                    raise ContractError("external live-document identity substitution")
            predecessors[path] = observed
        if effect.startswith("member_copy:") and pending is None:
            member = next((m for m in self.plan.grant["members"] if self.plan.staged_path(m) == target), None)
            if member is None:
                raise ContractError("ungranted copy intent")
            if member["type"] == "file":
                old = predecessors.get(target)
                if old is None or old != self.journal.value["objects"].get(target):
                    raise ContractError("copy intent predecessor is not journal-owned")
                if self.backend.read(target, maximum=1) != b"":
                    raise ContractError("uncompleted copy has foreign bytes")
                if self.backend.exists(target) != old:
                    raise ContractError("copy intent predecessor changed during byte check")
        intent = pending or {"effect":effect,"add":list(add),"remove":list(remove),
                  "moved":list(moved or ()),"predecessors":predecessors}
        if pending is None:
            self.journal.commit(self.journal.value["phase"],pending_effect=intent,phase_boundary=False,
                                boundary="intent_journal:"+effect)
        self.backend.expect(self.journal.value["objects"])
        operation()
        self.journal.hit("applied:effect:"+effect)
        self._finish_effect(intent)
        self.journal.hit("post:effect:"+effect)

    def _finish_effect(self, intent):
        objects = dict(self.journal.value["objects"])
        for path in intent["remove"]:
            objects.pop(path,None)
        if intent["moved"]:
            source,target=intent["moved"]
            for path in tuple(objects):
                if path==source or path.startswith(source+"/"):
                    objects[target+path[len(source):]]=objects.pop(path)
        for path in intent["add"]:
            observed=self.backend.exists(path)
            previous=intent["predecessors"].get(path)
            if observed is not None:
                if intent["effect"] == "namespace_mkdir:" + path and previous is not None and observed != previous:
                    raise ContractError("namespace reuse cannot change retained directory metadata")
                if previous is not None and any(previous[k]!=observed[k] for k in ("dev","ino","mount","type","nlink")):
                    raise ContractError("effect cannot adopt substituted target identity")
                objects[path]=observed
        completed=set(self.journal.value["completed_effects"])|{intent["effect"]}
        self.journal.commit(self.journal.value["phase"],objects=objects,completed=completed,
                            phase_boundary=False,boundary="effect_journal:"+intent["effect"])
        self.backend.expect(objects)

    def _resolve_pending(self):
        intent=self.journal.value["pending_effect"]
        if intent is None: return False
        effect=intent["effect"]
        kind,path=effect.split(":",1)
        if intent["moved"]:
            source,target=intent["moved"]
            old=intent["predecessors"].get(source)
            left,right=self.backend.exists(source),self.backend.exists(target)
            if left==old and right is None: return False
            if left is not None or old is None or right!=old:
                raise ContractError("pending publication identity ambiguous")
            # Every descendant remains bound to the same pre-publication inode.
            for child,expected in self.journal.value["objects"].items():
                if child.startswith(source+"/") and self.backend.exists(target+child[len(source):])!=expected:
                    raise ContractError("pending publication descendant substitution")
        elif intent["remove"]:
            states=[(p,self.backend.exists(p),intent["predecessors"].get(p)) for p in intent["remove"]]
            if any(now is not None and now!=old for p,now,old in states):
                raise ContractError("pending removal target substituted")
            if any(now is not None for p,now,old in states): return False
        elif kind in ("member_create","policy_stage_create","namespace_mkdir","runtime_lock_create"):
            observed=self.backend.exists(path)
            old=intent["predecessors"].get(path)
            if observed is None: return False
            if old is not None and observed==old: return False
            raise ContractError("PENDING_CREATE_IDENTITY_NOT_PROVEN: preserve unjournaled object")
        elif kind in ("member_copy","metadata_seal"):
            old=intent["predecessors"].get(path)
            now=self.backend.exists(path)
            if old is None or now is None or any(now[k]!=old[k] for k in ("dev","ino","mount","nlink","type")):
                raise ContractError("pending mutation predecessor substitution")
            if now==old and kind=="metadata_seal": return False
            if kind=="member_copy":
                member=next((m for m in self.plan.grant["members"] if self.plan.staged_path(m)==path),None)
                if member is None: raise ContractError("ungranted pending copy")
                if now != old:
                    raise ContractError("pending copy predecessor metadata substitution")
                if member["type"] == "file":
                    raw = self.backend.read(path, maximum=member["size"]+1)
                    if self.backend.exists(path) != old:
                        raise ContractError("pending copy identity changed during byte check")
                    if raw == b"" and member["size"] != 0:
                        return False
                    if len(raw) != member["size"] or hashlib.sha256(raw).hexdigest() != member["sha256"]:
                        raise ContractError("effect artifact byte substitution")
                else:
                    self._verify_bytes(path,member)
                if self.backend.exists(path) != old:
                    raise ContractError("pending copy predecessor changed during verification")
            else:
                metadata=self._seal_metadata(path)
                if any(now[k]!=metadata[k] for k in ("uid","gid","mode")):
                    raise ContractError("pending seal unapproved metadata delta")
        else:
            return False
        self._finish_effect(intent)
        return True

    def _seal_metadata(self,path):
        for member in self.plan.grant["members"]:
            if path==self.plan.staged_path(member): return member
        for item in self.plan.grant["snapshot_envelope"]:
            if path==self.plan.stage+"/snapshot/"+item["name"]: return item
        mode=0o555 if path==self.plan.stage+"/snapshot" or path.startswith(self.plan.stage+"/snapshot/") or path==self.plan.broker or path.startswith(self.plan.broker+"/") else 0o440 if path==self.plan.stage+"/policy" else 0o600 if path==LIVE_LOCK else 0o700 if path==self.plan.grant["authority"]["scratch_parent"][1:] else 0o400
        return {"uid":0,"gid":0,"mode":mode}

    def _verify_bytes(self,path,expected):
        if expected["type"]=="file":
            raw=self.backend.read(path,maximum=expected["size"]+1)
            if len(raw)!=expected["size"] or hashlib.sha256(raw).hexdigest()!=expected["sha256"]:
                raise ContractError("effect artifact byte substitution")

    def _ensure_directory(self, path):
        existing = self.backend.exists(path)
        if existing is not None:
            if existing["type"] != "directory" or existing["mount"] != self.backend.mount:
                raise ContractError("directory substituted")
            retained = self.plan.retained_directory_identities.get(path)
            if retained is not None and existing != retained:
                raise ContractError("retained directory changed before reuse: " + path)
            return
        self.backend.mkdir(path)

    def _prepare(self):
        # A stale source artifact cannot authorize journal promotion or even
        # namespace preparation. The same held-source check repeats after load.
        self._revalidate_source()
        # Namespace creation has no payload/publication authority and is itself
        # bounded by the externally authenticated grant and explicit fault points.
        if not self.backend.capability.fake_root:
            interpreter = self.backend.identity("usr/bin/python3.14")
            if interpreter["uid"] != 0 or interpreter["gid"] != 0 or interpreter["type"] != "file" or interpreter["nlink"] != 1:
                raise ContractError("bootstrap interpreter identity invalid")
            raw = self.backend.read("usr/bin/python3.14", maximum=64*1024*1024)
            if hashlib.sha256(raw).hexdigest() != self.plan.grant["bootstrap_python_sha256"]:
                raise ContractError("external bootstrap interpreter digest mismatch")
        bootstrap = set(_parents(JOURNAL_PATH))
        for path in self.plan.directories:
            if path in bootstrap:
                self._effect("namespace_mkdir:" + path, lambda p=path: self._ensure_directory(p))
        self._effect("install_lock:" + INSTALL_LOCK,
            lambda: self._lock_install())
        self.journal.load()
        if self.journal.value is None:
            self.journal.commit("install_prepared")
        else:
            self._resolve_pending()
            self.journal.revalidate_objects()
        # External source identity remains part of the exact recovery request.
        # A previously copied stage does not authorize adopting changed inputs.
        self._revalidate_source()
        for path in self.plan.directories:
            if path not in bootstrap:
                self._effect("namespace_mkdir:" + path, lambda p=path: self._ensure_directory(p), add=(path,))

    def _revalidate_source(self):
        self.plan.source.check()
        for member in self.plan.grant["members"]:
            if _source_identity(self.plan.source, member["source"]) != member["source_identity"]:
                raise ContractError("recovery source descriptor identity drift")
            self.plan.source.read_exact(member["source"], expected_sha256=member["sha256"], expected_size=member["size"])

    def _lock_install(self):
        if not self.backend.lock(INSTALL_LOCK):
            raise ContractError("install transaction already locked")

    def _copy_member(self, member):
        stage = self.plan.staged_path(member)
        # Create/copy are distinct effects. An identity-bound empty file may be
        # resumed, whereas any unknown preexisting file is foreign residue.
        self._effect("member_create:" + stage,
            lambda: self.backend.create(stage, b"", link_target=member["target"] if member["type"] == "symlink" else None), add=(stage,))
        def copy():
            self.plan.source.check()
            if _source_identity(self.plan.source, member["source"]) != member["source_identity"]:
                raise ContractError("source identity changed during descriptor copy")
            if member["type"] == "symlink":
                return
            raw = self.plan.source.read_exact(member["source"], expected_sha256=member["sha256"], expected_size=member["size"])
            fd, parent, name, before = self.backend._regular(stage, os.O_WRONLY)
            try:
                if self.backend.identity(stage) != self.journal.value["objects"].get(stage):
                    raise ContractError("copy target inode substituted")
                if before.st_size != 0:
                    raise ContractError("uncompleted copy has foreign bytes")
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise ContractError("short descriptor copy")
                    view = view[count:]
            finally:
                os.close(fd)
                self.backend.release_parent(parent)
        self._effect("member_copy:" + stage, copy, add=(stage,))
        self._effect("metadata_seal:" + stage,
            lambda: self.backend.seal(stage, member["uid"], member["gid"], member["mode"]), add=(stage,))
        self._effect("file_fsync:" + stage, lambda: self.backend.fsync(stage))
        self._effect("parent_fsync:" + stage, lambda: self.backend.fsync(stage, parent_only=True))

    def _phase(self, phase):
        if self.journal.value["phase"] == phase:
            return
        self.journal.commit(phase)

    def _verify_file(self, path, expected):
        observed = self.backend.identity(path)
        for key in ("uid", "gid", "mode", "type"):
            if observed[key] != expected[key]:
                raise ContractError("installed member metadata mismatch: " + path)
        if observed["nlink"] != 1 or observed["mount"] != self.backend.mount:
            raise ContractError("installed member link/mount mismatch")
        if expected["type"] == "file":
            raw = self.backend.read(path, maximum=expected["size"] + 1)
            if len(raw) != expected["size"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                raise ContractError("installed member bytes mismatch: " + path)
        else:
            if isinstance(self.backend, RecordingBackend):
                target = self.backend._metadata.get(path, {}).get("target")
            else:
                parent, name = self.backend._parent(path)
                try:
                    target = os.readlink(name, dir_fd=parent)
                    self.backend.check_parent(parent)
                finally:
                    self.backend.release_parent(parent)
            if target != expected["target"]:
                raise ContractError("installed symlink mismatch")

    def verify_installed(self, *, policy=False):
        self.journal.revalidate_objects()
        scratch = self.plan.grant["authority"]["scratch_parent"][1:]
        if self.backend.identity(scratch)["uid"] != 0 or self.backend.identity(scratch)["gid"] != 0 or self.backend.identity(scratch)["mode"] != 0o700:
            raise ContractError("runtime scratch prerequisite invalid")
        lock = self.backend.identity(LIVE_LOCK)
        if any(lock[k] != v for k, v in {"uid":0,"gid":0,"mode":0o600,"type":"file","nlink":1}.items()):
            raise ContractError("runtime live-lock prerequisite invalid")
        for path, expected in self.plan.files.items():
            if path != self.plan.policy_path or policy:
                self._verify_file(path, expected)
        index_path = self.plan.broker + "/package-index.v1.json"
        index = parse_canonical_object(self.backend.read(index_path), INDEX_KEYS,
            schema="friday.package-index.v1", expected_sha256=self.plan.grant["bootstrap_trust"]["runtime_index_sha256"])
        expected_names = sorted(name + ".py" for name in BOOTSTRAP_RUNTIME_MODULES)
        if [item.get("path") for item in index["members"]] != expected_names:
            raise ContractError("installed runtime closed module set incomplete/foreign")
        for item in index["members"]:
            exact_keys(item, INDEX_MEMBER_KEYS)
            path = self.plan.broker + "/" + item["path"]
            expected = self.plan.files[path]
            if item["role"] != "runtime" or item["mode"] != 0o444 or any(item[k] != expected[k] for k in ("mode", "size", "sha256")):
                raise ContractError("installed runtime index/member disagrees with grant")
        if self.plan.grant["bootstrap_trust"]["runtime_index_sha256"] != self.plan.grant["authority"]["broker_bundle_sha256"]:
            raise ContractError("runtime bundle external trust pins disagree")
        for root in (self.plan.snapshot, self.plan.broker):
            expected = {p for p in self.plan.files if p.startswith(root + "/")}
            expected.update(p for p in self.plan.paths if p.startswith(root + "/") and p not in self.plan.files)
            if set(self.backend.inventory(root)) != expected:
                raise ContractError("published tree extra/missing inventory")
        self._verify_snapshot_at(self.plan.snapshot)

    def _verify_snapshot_at(self, path):
        rootfd_parent, rootname = self.backend._parent(path)
        fd = None
        try:
            fd = os.open(rootname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd_parent)
            self.backend.check_parent(rootfd_parent)
            adapter = _MetadataObservation(self.backend) if isinstance(self.backend, RecordingBackend) else None
            from manifest import installation_binding
            binding = installation_binding(_journal_bytes(self.journal.value),
                expected_install_identity_sha256=self.plan.identity_sha256,
                expected_install_grant_sha256=self.plan.expected_grant_sha256,
                expected_manifest_sha256=self.plan.grant["snapshot_manifest_sha256"], snapshot_path=self.plan.snapshot, purpose="staged")
            held = verify_snapshot(fd, self.plan.manifest, expected_uid=0, expected_gid=0, backend=adapter,
                expected_provenance_sha256=self.plan.grant["provenance_sha256"], installation_binding=binding)
            try:
                # verify_snapshot already revalidates before returning a held
                # descriptor. No duplicate scan on unchanged bytes is needed.
                self.backend.check_parent(rootfd_parent)
            finally:
                held.close()
        finally:
            if fd is not None: os.close(fd)
            self.backend.release_parent(rootfd_parent)

    def _publish_config(self, label, target, value):
        stage = self.plan.stage + "/" + label
        self._effect("member_create:" + stage, lambda: self.backend.create(stage, canonical_bytes(value)), add=(stage,))
        self._effect("metadata_seal:" + stage, lambda: self.backend.seal(stage, 0, 0, 0o400), add=(stage,))
        self._effect("file_fsync:" + stage, lambda: self.backend.fsync(stage))
        self._effect("authority_publish:" + target, lambda: self.backend.publish(stage, target,
            validate_source=lambda:self._verify_file(stage,self.plan.files[target])), moved=(stage, target))

    def _cleanup_stage(self, path):
        if self.backend.exists(path) is not None:
            if self.backend.inventory(path):
                raise ContractError("private stage residue")
            self.backend.unlink(path, self.backend.identity(path))

    def install(self):
        if self.backend.capability.operation != "root-install":
            raise ContractError("separate root-install authority required")
        self._prepare()
        phase = self.journal.value["phase"]
        if phase in REMOVE_PHASES:
            raise ContractError("installation cannot resume after revocation/removal")
        if phase == "installed_not_live":
            self.verify_installed(policy=True)
            if self.backend.exists(self.plan.live_grant_path) is not None:
                raise ContractError("install is not live authority")
            return self.journal.value
        start = INSTALL_PHASES.index(phase)
        for current in INSTALL_PHASES[start:]:
            self._phase(current)
            if current == "install_prepared":
                scratch = self.plan.grant["authority"]["scratch_parent"][1:]
                self._effect("metadata_seal:"+scratch, lambda: self.backend.seal(scratch,0,0,0o700), add=(scratch,))
                def runtime_lock():
                    if not self.backend.lock(LIVE_LOCK):
                        raise ContractError("runtime lock unavailable during install")
                    self.backend.seal(LIVE_LOCK,0,0,0o600)
                self._effect("runtime_lock_create:"+LIVE_LOCK,runtime_lock,add=(LIVE_LOCK,))
                for item, raw in zip(self.plan.grant["snapshot_envelope"],(self.plan.manifest_raw,self.plan.provenance_raw)):
                    stage = self.plan.stage+"/snapshot/"+item["name"]
                    self._effect("member_create:"+stage,lambda p=stage,r=raw:self.backend.create(p,r),add=(stage,))
                    self._effect("metadata_seal:"+stage,lambda p=stage:self.backend.seal(p,0,0,0o444),add=(stage,))
                    self._effect("file_fsync:"+stage,lambda p=stage:self.backend.fsync(p))
                for member in self.plan.grant["members"]:
                    self._copy_member(member)
            elif current == "snapshot_publishing":
                stage = self.plan.stage + "/snapshot"
                # Snapshot directories are sealed according to exact manifest.
                for member in sorted((m for m in self.plan.manifest["members"] if m["type"] == "directory"), key=lambda m: -m["path"].count("/")):
                    path = stage + "/" + member["path"]
                    self._effect("metadata_seal:" + path,
                        lambda p=path,m=member: self.backend.seal(p,m["uid"],m["gid"],m["mode"]), add=(path,))
                    self._effect("file_fsync:" + path, lambda p=path: self.backend.fsync(p))
                self._effect("metadata_seal:" + stage, lambda: self.backend.seal(stage,0,0,0o555), add=(stage,))
                self._effect("file_fsync:" + stage, lambda: self.backend.fsync(stage))
                self._effect("prerequisites_verify:" + stage, lambda: self._verify_snapshot_at(stage))
                self._effect("snapshot_publish:" + self.plan.snapshot,
                    lambda: self.backend.publish(stage, self.plan.snapshot,validate_source=lambda:self._verify_snapshot_at(stage)), moved=(stage, self.plan.snapshot))
            elif current == "broker_publishing":
                for member in self.plan.grant["members"]:
                    target = member["destination"][1:]
                    if target.startswith(self.plan.broker + "/"):
                        stage = self.plan.staged_path(member)
                        self._effect("broker_publish:" + target, lambda s=stage,t=target: self.backend.publish(s,t,
                            validate_source=lambda:self._verify_file(s,self.plan.files[t])), moved=(stage,target))
            elif current == "authority_publishing":
                self._publish_config("authority", self.plan.authority_path, self.plan.grant["authority"])
                self._publish_config("trust", self.plan.trust_path, self.plan.grant["bootstrap_trust"])
                for path in sorted((p for p in self.plan.directories if p == self.plan.broker or p.startswith(self.plan.broker + "/")), key=lambda p: -p.count("/")):
                    self._effect("metadata_seal:" + path, lambda p=path: self.backend.seal(p,0,0,0o555), add=(path,))
                    self._effect("file_fsync:" + path, lambda p=path: self.backend.fsync(p))
            elif current == "policy_publishing":
                self._effect("prerequisites_verify:" + self.plan.snapshot, self.verify_installed)
                self._effect("private_stage_cleanup:" + self.plan.stage + "/broker",
                    lambda: self._cleanup_stage(self.plan.stage + "/broker"), remove=(self.plan.stage + "/broker",))
                stage = self.plan.stage + "/policy"
                self._effect("policy_stage_create:" + stage, lambda: self.backend.create(stage, self.plan.policy), add=(stage,))
                self._effect("metadata_seal:" + stage, lambda: self.backend.seal(stage,0,0,0o440), add=(stage,))
                self._effect("file_fsync:" + stage, lambda: self.backend.fsync(stage))
                self._effect("policy_stage_validate:" + stage, lambda: self.backend.validate_policy(stage, self.plan.policy))
                def publish_policy():
                    expected = {stage}
                    if set(self.backend.inventory(self.plan.stage)) != expected:
                        raise ContractError("private-stage residue before policy publication")
                    self.verify_installed()
                    self.backend.publish(stage,self.plan.policy_path,
                        validate_source=lambda:self._verify_file(stage,self.plan.files[self.plan.policy_path]))
                self._effect("policy_publish:" + self.plan.policy_path,
                    publish_policy, moved=(stage,self.plan.policy_path))
            elif current == "policy_published":
                self._effect("private_stage_cleanup:" + self.plan.stage,
                    lambda: self._cleanup_stage(self.plan.stage), remove=(self.plan.stage,))
                self._effect("installed_verify:" + self.plan.snapshot, lambda: self.verify_installed(policy=True))
        if self.backend.exists(self.plan.live_grant_path) is not None:
            raise ContractError("installer must not publish a live grant")
        return self.journal.value
