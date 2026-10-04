"""Actual revoke-first removal, generated effects, fences and audit survival."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest

from canonical import ContractError
from install import Installer, InstallJournal, INSTALL_PHASES, REMOVE_PHASES, removal_fault_points
from install_bootstrap import NAMESPACE, PERMANENT_PREFIXES, JOURNAL_PATH
from recover import recover_install
from uninstall import Remover
from test_install_recovery import fixture, removal_backend, FaultAt, Crash, _fault_signature, write_private
from support import observe, matrix
from install_controls import (remove_contract, MemorySyscallReplay, remove_prefixes,
    retain_prefixes, POLICY_SUBSTITUTIONS, install_prefixes,
    PENDING_COPY_REMOVE_STATES, PENDING_COPY_REMOVE_POSITIVES)
from install_bootstrap import TEST_TOKEN, RecordingBackend, LIVE_LOCK


def permanent_records(plan, backend):
    # Ordinary synthetic durable records, explicitly outside removable paths.
    root = Path(backend.capability.root)
    result = {}
    for base, name, raw in ((PERMANENT_PREFIXES[0], "consumed.v1", b"synthetic consumed permanent\n"),
                            (PERMANENT_PREFIXES[1], "terminal.v1.json", b"synthetic terminal evidence\n")):
        path = root / base / name
        write_private(path, raw)
        result[str(path)] = hashlib.sha256(raw).hexdigest()
    return result


class UninstallTests(unittest.TestCase):
    def test_exact_live_three_documents_and_fresh_suffix_fence(self):
        from canonical import canonical_bytes
        for kind in ("live-documents","suffix-fence","journal-lock"):
            plan,original,_=fixture("remove-source-controls"); root=original.capability.root; original.close()
            try:
                with MemorySyscallReplay(root):
                    backend=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN))
                    try:
                        if kind=="journal-lock":
                            with self.assertRaises(Crash): Installer(plan,backend,FaultAt("post:journal_stage:payloads_staged")).install()
                            state=backend.export_state()
                            other=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN)); other.import_state(state)
                            try:
                                staged=backend.read(__import__("install_bootstrap").JOURNAL_STAGE,maximum=64<<20)
                                with self.assertRaisesRegex(ContractError,"locked"): recover_install(plan,other)
                                self.assertEqual(staged,backend.read(__import__("install_bootstrap").JOURNAL_STAGE,maximum=64<<20))
                                self.assertFalse(any(e=="journal_replace" for e,p in other.events))
                                observe("remove-negative","locked-journal-promotion","PASS",fresh_backend=True,stage_unchanged=True)
                            finally: other.close()
                        else:
                            Installer(plan,backend).install()
                            live=[]
                            if kind=="live-documents":
                                for path in sorted((plan.live_grant_path,plan.live_pin_path,plan.live_effects_path)):
                                    raw=canonical_bytes({"inert-document":path})
                                    backend.create(path,raw); backend.seal(path,0,0,0o400)
                                    live.append({"path":path,"identity":backend.identity(path),"sha256":hashlib.sha256(raw).hexdigest()})
                            backend=removal_backend(plan,backend,live_grants=live)
                            if kind=="suffix-fence":
                                with self.assertRaises(Crash): Remover(plan,backend,FaultAt("post:phase:private_stages_removing")).remove()
                                state=backend.export_state(); capability=backend.capability; backend.close()
                                backend=RecordingBackend(capability); backend.import_state(state)
                                backend.active_run=True
                                with self.assertRaisesRegex(ContractError,"active execution fenced"): recover_install(plan,backend)
                                self.assertIsNotNone(backend.exists(LIVE_LOCK))
                                self.assertFalse(any(e=="unlink" for e,p in backend.events))
                                backend.active_run=False
                                self.assertEqual(recover_install(plan,backend)["phase"],"removed")
                                observe("remove-negative","fresh-private-suffix-fence","PASS",fresh_backend=True,lock_preserved_on_refusal=True)
                            else:
                                self.assertEqual(Remover(plan,backend).remove()["phase"],"removed")
                                for item in live: self.assertIsNone(backend.exists(item["path"]))
                                observe("remove","exact-live-three-documents","PASS",exact_docs=3)
                    finally: backend.close()
            finally: plan.source.close()
    def test_direct_removal_pending_copy_catalogue(self):
        """Authentic pending prefixes, fresh revoke capability, inert syscalls."""
        from canonical import canonical_bytes
        from install_bootstrap import JOURNAL_STAGE, INSTALL_LOCK
        import json
        covered = []
        for empty_grant in (False, True):
            plan, original, _ = fixture("remove-direct-pending-copy", with_empty_member=empty_grant)
            root = original.capability.root
            original.close()
            try:
                with MemorySyscallReplay(root) as replay:
                    seed = RecordingBackend(plan.capability(root, fake_root=True, test_token=TEST_TOKEN))
                    try:
                        snapshots, install_trace = install_prefixes(plan, seed, replay)
                    finally:
                        seed.close()
                    member = next(m for m in plan.grant["members"] if m["type"] == "file"
                        and (m["size"] == 0 if empty_grant else m["size"] > 1))
                    path = plan.staged_path(member)
                    copying = "member_copy:" + path
                    granted = plan.source.read_exact(member["source"],
                        expected_sha256=member["sha256"], expected_size=member["size"])
                    selected = ("empty-granted",) if empty_grant else tuple(
                        key for key in PENDING_COPY_REMOVE_STATES if key != "empty-granted")
                    for state_name in selected:
                        point = ("post:intent_journal:" + copying if state_name in ("empty-stage", "empty-granted") else
                            "applied:effect:" + copying if state_name in ("full-applied", "partial-then-granted", "wrong-then-granted") else
                            "pre:effect:" + copying if state_name == "pre-intent-foreign" else
                            "post:publish:intent_journal:" + copying)
                        prefix = snapshots[point]
                        replay.restore(prefix)
                        seed = RecordingBackend(plan.capability(root, fake_root=True, test_token=TEST_TOKEN))
                        seed.import_state(prefix["metadata"])
                        backend = removal_backend(plan, seed)
                        self.assertEqual(backend.capability.operation, "revoke-remove")
                        self.assertTrue(backend.syscalls.inert)
                        # Actual prefix validation precedes changes to owned inert
                        # byte/identity data. No already-recovered install is used.
                        journal_target = JOURNAL_STAGE if state_name in ("empty-stage", "empty-granted") else JOURNAL_PATH
                        prefix_raw = replay.nodes[root + "/" + journal_target]["raw"]
                        authenticated = InstallJournal(plan, None)._parse(prefix_raw)
                        self.assertEqual(authenticated["pending_effect"]["effect"], copying)
                        self.assertNotIn(copying, authenticated["completed_effects"])
                        node = replay.nodes[root + "/" + path]
                        permanent = {}
                        for base, name, raw in ((PERMANENT_PREFIXES[0], "consumed.v1", b"inert permanent consumed\\n"),
                                               (PERMANENT_PREFIXES[1], "terminal.v1.json", b"inert permanent evidence\\n")):
                            permanent_path = root + "/" + base + "/" + name
                            replay.nodes[permanent_path] = replay.node("file", 0o400, raw)
                            permanent[permanent_path] = copy.deepcopy(replay.nodes[permanent_path])
                        samples = []
                        if state_name == "partial":
                            node["raw"] = granted[:1]
                            replay.touch(root + "/" + path)
                        elif state_name in ("wrong-digest", "pre-intent-foreign"):
                            node["raw"] = bytes([granted[0] ^ 1]) + granted[1:]
                            replay.touch(root + "/" + path)
                        elif state_name in ("changed-inode", "changed-metadata"):
                            observed = backend.identity(path)
                            axis = "ino" if state_name == "changed-inode" else "uid"
                            backend.model_substitution(path, **{axis: observed[axis] + 1})
                        elif state_name in ("identity-drift-on-read", "partial-then-granted", "wrong-then-granted"):
                            original_read = backend.read
                            def altered_owned_sample(target, **kwargs):
                                raw = original_read(target, **kwargs)
                                if target == path:
                                    samples.append(target)
                                    if len(samples) == 1:
                                        if state_name == "identity-drift-on-read":
                                            current = backend.identity(path)
                                            backend.model_substitution(path, ino=current["ino"] + 1)
                                        else:
                                            return raw[:1] if state_name == "partial-then-granted" else bytes([raw[0] ^ 1]) + raw[1:]
                                return raw
                            backend.read = altered_owned_sample
                        before_bytes = node["raw"]
                        before_journal = {target: replay.nodes[root + "/" + target]["raw"]
                            for target in (JOURNAL_PATH, JOURNAL_STAGE) if root + "/" + target in replay.nodes}
                        consumers, ordered = [], ["Remover.remove"]
                        removal_hooks = []
                        remover = Remover(plan, backend, removal_hooks.append)
                        def track(target, attribute, label):
                            original_call = getattr(target, attribute)
                            def call(*args, **kwargs):
                                consumers.append({"method": label, "event": "enter"})
                                ordered.append(label)
                                try:
                                    result = original_call(*args, **kwargs)
                                except ContractError as error:
                                    consumers.append({"method": label, "event": "refused", "reason": str(error)})
                                    raise
                                consumers.append({"method": label, "event": "return",
                                    "value": result if type(result) is bool else None})
                                return result
                            setattr(target, attribute, call)
                        for target, attribute, label in (
                            (remover, "_lock_install", "Installer._lock_install"),
                            (remover.journal, "load", "InstallJournal.load"),
                            (remover, "_resolve_pending", "Installer._resolve_pending"),
                            (remover, "_revoke_policy", "Remover._revoke_policy"),
                            (remover, "_fence", "Remover._fence"),
                            (remover, "_remove_private", "Remover._remove_private"),
                            (remover, "zero_residue", "Remover.zero_residue")):
                            track(target, attribute, label)
                        original_commit = remover.journal.commit
                        def commit(phase, **kwargs):
                            if kwargs.get("cancelled_effect") == copying:
                                ordered.append("InstallJournal.commit:cancel-copy")
                            elif copying in kwargs.get("completed", ()) and copying not in remover.journal.value["completed_effects"]:
                                ordered.append("InstallJournal.commit:complete-copy")
                            if kwargs.get("phase_boundary", True) and phase in REMOVE_PHASES:
                                ordered.append("InstallJournal.commit:" + phase)
                            return original_commit(phase, **kwargs)
                        remover.journal.commit = commit
                        try:
                            if state_name in PENDING_COPY_REMOVE_POSITIVES:
                                terminal = remover.remove()
                                self.assertEqual(terminal["phase"], "removed")
                                self.assertIsNone(backend.exists(path))
                                applied = state_name in ("full-applied", "empty-granted")
                                self.assertEqual(copying in terminal["completed_effects"], applied)
                                cancelled = [r for r in terminal["records"] if r["cancelled_effect"] == copying]
                                self.assertEqual(len(cancelled), 0 if applied else 1)
                                expected_calls = ["Remover.remove", "Installer._lock_install", "InstallJournal.load",
                                    "Installer._resolve_pending", "InstallJournal.commit:" + ("complete-copy" if applied else "cancel-copy"),
                                    "InstallJournal.commit:remove_prepared", "InstallJournal.commit:policy_revoking",
                                    "Remover._revoke_policy", "InstallJournal.commit:policy_revoked",
                                    "InstallJournal.commit:live_grant_removing", "Remover._fence",
                                    "InstallJournal.commit:authority_removing", "InstallJournal.commit:snapshot_removing",
                                    "InstallJournal.commit:broker_removing", "InstallJournal.commit:private_stages_removing",
                                    "Remover._remove_private", "Remover.zero_residue", "InstallJournal.commit:removed"]
                                self.assertEqual(ordered, expected_calls)
                                self.assertFalse(any(effect in ("publish", "validate_policy") for effect, _ in backend.events))
                                first_payload_delete = next((i for i, event in enumerate(backend.events) if event[0] == "unlink"), len(backend.events))
                                revocation_index = next(i for i, event in enumerate(backend.events) if event[0] == "validate_revocation")
                                self.assertLess(revocation_index, first_payload_delete)
                                self.assertIn("post:effect:execution_fence:" + LIVE_LOCK, removal_hooks)
                                self.assertEqual(InstallJournal(plan, None)._parse(canonical_bytes(terminal)), terminal)
                            else:
                                reasons = {
                                    "partial": "effect artifact byte substitution",
                                    "wrong-digest": "effect artifact byte substitution",
                                    "changed-inode": "pending mutation predecessor substitution",
                                    "changed-metadata": "pending copy predecessor metadata substitution",
                                    "identity-drift-on-read": "pending copy identity changed during byte check",
                                    "pre-intent-foreign": "effect artifact byte substitution",
                                    "partial-then-granted": "effect artifact byte substitution",
                                    "wrong-then-granted": "effect artifact byte substitution"}
                                with self.assertRaisesRegex(ContractError, reasons[state_name]):
                                    remover.remove()
                                self.assertEqual(ordered, ["Remover.remove", "Installer._lock_install",
                                    "InstallJournal.load", "Installer._resolve_pending"])
                                self.assertEqual(node["raw"], before_bytes)
                                self.assertIsNotNone(backend.exists(path))
                                self.assertFalse(backend.events)
                                for target, raw in before_journal.items():
                                    self.assertEqual(replay.nodes[root + "/" + target]["raw"], raw)
                                if state_name in ("partial-then-granted", "wrong-then-granted"):
                                    self.assertEqual(len(samples), 1)
                            for target, original_node in permanent.items():
                                self.assertEqual(replay.nodes[target], original_node)
                            self.assertIsNone(backend.exists(plan.policy_path))
                            observe("remove-direct-pending-copy", state_name, "PASS",
                                authentic_pending_prefix=point, prefix_hook_index=prefix["hook_index"],
                                prefix_journal_sha256=hashlib.sha256(prefix_raw).hexdigest(),
                                ordered_consumers=ordered, actual_consumer_calls=consumers,
                                actual_removal_hooks=removal_hooks,
                                object_before_sha256=hashlib.sha256(before_bytes).hexdigest(),
                                object_preserved_on_refusal=state_name not in PENDING_COPY_REMOVE_POSITIVES,
                                copy_resolution="APPLIED" if state_name in ("full-applied", "empty-granted") else
                                    "CANCELLED_UNAPPLIED" if state_name in PENDING_COPY_REMOVE_POSITIVES else "REFUSED_PRESERVED",
                                distinct_fresh_revoke_remove_capability=True, permanent_records_exact=True,
                                policy_publication=False, no_installed_recovery_before_removal=True,
                                inert_syscall_boundary=True, native_durability_credit=False)
                            covered.append(state_name)
                        finally:
                            backend.close()
            finally:
                plan.source.close()
        matrix("remove-direct-pending-copy", remove_contract()["matrices"]["remove-direct-pending-copy"], covered)

    def test_complete_revoke_first_zero_residue_and_permanent_audit(self):
        plan, backend, _ = fixture("remove-complete")
        try:
            Installer(plan, backend).install()
            permanent = permanent_records(plan, backend)
            with self.assertRaises(ContractError):
                Remover(plan, backend).remove()
            backend = removal_backend(plan, backend)
            terminal = Remover(plan, backend).remove()
            self.assertEqual(terminal["phase"], "removed")
            deletions = [path for effect, path in backend.events if effect == "unlink"]
            self.assertEqual(deletions[0], plan.policy_path)
            for name, expected in permanent.items():
                self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(), expected)
            Remover(plan, backend).zero_residue()
            with self.assertRaises(ContractError):
                backend.unlink(PERMANENT_PREFIXES[0] + "/consumed.v1", backend.identity(PERMANENT_PREFIXES[0] + "/consumed.v1"))
            self.assertEqual(recover_install(plan, backend)["phase"], "removed")
            observe("remove", "revoke-first-zero-residue-audit", "PASS", phase="removed")
        finally:
            backend.close()
            plan.source.close()

    def test_each_remove_phase_identity_and_no_install_republication(self):
        plan,original,_=fixture("remove-phase-prefix"); root=original.capability.root; original.close()
        covered=[]; expected=remove_contract()["matrices"]["remove-phase-identity-no-republish"]
        try:
            with MemorySyscallReplay(root) as replay:
                backend=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN))
                try:
                    Installer(plan,backend).install(); backend=removal_backend(plan,backend)
                    snapshots,trace=remove_prefixes(plan,backend,replay)
                finally: backend.close()
                retain_prefixes(root,["post:phase:"+phase for phase in REMOVE_PHASES],snapshots,trace)
                for phase in REMOVE_PHASES:
                    for action in ("foreign","resume"):
                        key=phase+":"+action; snapshot=snapshots["post:phase:"+phase]; replay.restore(snapshot)
                        seed=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN)); seed.import_state(snapshot["metadata"])
                        backend=removal_backend(plan,seed)
                        try:
                            if action=="foreign":
                                changed=copy.copy(plan); changed.identity_sha256="f"*64
                                with self.assertRaises(ContractError): recover_install(changed,backend)
                            else: self.assertEqual(recover_install(plan,backend)["phase"],"removed")
                            self.assertFalse(any(effect=="publish" for effect,path in backend.events))
                            covered.append(key); observe("remove-phase",key,"PASS",modeled_crash=True,policy_republished=False)
                        finally: backend.close()
            matrix("remove-phase-identity-no-republish",expected,covered)
        finally: plan.source.close()

    def test_policy_substitution_prevents_every_other_removal(self):
        plan, original, _ = fixture("remove-policy-hostile")
        root = original.capability.root
        original.close()
        try:
            with MemorySyscallReplay(root) as replay:
                backend = RecordingBackend(plan.capability(root, fake_root=True, test_token=TEST_TOKEN))
                try:
                    Installer(plan, backend).install()
                    snapshot = replay.snapshot(backend)
                finally: backend.close()
                covered = []
                for kind in POLICY_SUBSTITUTIONS:
                    with self.subTest(kind=kind):
                        replay.restore(snapshot)
                        backend = RecordingBackend(plan.capability(root, fake_root=True, test_token=TEST_TOKEN))
                        backend.import_state(snapshot["metadata"])
                        backend = removal_backend(plan, backend)
                        try:
                            mapping = {"uid": {"uid": 1000}, "gid": {"gid": 1000}, "mode": {"mode": 0o600},
                                "inode": {"ino": backend.identity(plan.policy_path)["ino"] + 1}, "mount": {"mount": backend.mount + 1},
                                "nlink": {"nlink": 2}, "symlink": {"type": "symlink"}, "fifo": {"type": "fifo"},
                                "socket": {"type": "socket"}, "device": {"type": "device"}}
                            if kind == "digest":
                                node = replay.nodes[root + "/" + plan.policy_path]
                                node["raw"] = bytes([node["raw"][0] ^ 1]) + node["raw"][1:]
                            else:
                                backend.model_substitution(plan.policy_path, **mapping[kind])
                            with self.assertRaises(ContractError): Remover(plan, backend).remove()
                            self.assertFalse(any(effect == "unlink" for effect, _ in backend.events))
                            self.assertIsNotNone(backend.exists(plan.authority_path))
                            for path in set(plan.files) - {plan.policy_path}:
                                self.assertEqual(replay.nodes[root + "/" + path], snapshot["nodes"][root + "/" + path])
                                self.assertEqual(backend.export_state().get(path), snapshot["metadata"].get(path))
                            observe("remove-negative", "policy-substitution-" + kind, "PASS", payload_untouched=True,
                                actual_source_methods=True, modeled_syscalls=True, native_durability=False,
                                independent_prefix_restored=True, full_install_setup_count=1, negative_rows=11,
                                every_other_payload_member_checked=len(plan.files) - 1)
                            covered.append(kind)
                        finally: backend.close()
                matrix("remove-policy-substitution", remove_contract()["matrices"]["remove-policy-substitution"], covered)
        finally: plan.source.close()

    def test_active_execution_revoke_then_fence_and_resume(self):
        plan, backend, _ = fixture("remove-active")
        try:
            Installer(plan, backend).install()
            backend = removal_backend(plan, backend)
            backend.active_run = True
            with self.assertRaises(ContractError):
                Remover(plan, backend).remove()
            self.assertIsNone(backend.exists(plan.policy_path))
            self.assertIsNotNone(backend.exists(plan.snapshot))
            self.assertIsNotNone(backend.exists(plan.authority_path))
            self.assertEqual(InstallJournal(plan, backend).load()["phase"], "live_grant_removing")
            backend.active_run = False
            self.assertEqual(recover_install(plan, backend)["phase"], "removed")
            observe("remove-negative", "active-execution-fence", "PASS", revoked_before_fence=True)
        finally:
            backend.close()
            plan.source.close()

    def test_substituted_payload_foreign_residue_and_live_grant(self):
        for kind in ("payload-inode", "payload-mount", "extra-snapshot", "extra-stage", "foreign-live-grant"):
            with self.subTest(kind=kind):
                plan, backend, _ = fixture("remove-residue")
                try:
                    if kind == "extra-stage":
                        with self.assertRaises(Crash):
                            Installer(plan, backend, FaultAt("post:phase:payloads_staged")).install()
                        victim = Path(backend.capability.root) / plan.stage / "foreign"
                        write_private(victim, b"foreign private bytes")
                    else:
                        Installer(plan, backend).install()
                        if kind.startswith("payload-"):
                            target = next(p for p in plan.files if p.startswith(plan.snapshot + "/"))
                            victim = Path(backend.capability.root) / target
                            fields = {"ino": backend.identity(target)["ino"] + 1} if kind == "payload-inode" else {"mount": backend.mount + 1}
                            backend.model_substitution(target, **fields)
                        elif kind == "extra-snapshot":
                            victim = Path(backend.capability.root) / plan.snapshot / "foreign"
                            write_private(victim, b"foreign snapshot bytes")
                        else:
                            victim = Path(backend.capability.root) / plan.live_grant_path
                            write_private(victim, b"foreign unapproved live grant")
                    expected = hashlib.sha256(victim.read_bytes()).hexdigest()
                    backend = removal_backend(plan, backend)
                    with self.assertRaises(ContractError):
                        Remover(plan, backend).remove()
                    self.assertIsNone(backend.exists(plan.policy_path))
                    self.assertEqual(hashlib.sha256(victim.read_bytes()).hexdigest(), expected)
                    observe("remove-negative", kind, "PASS", foreign_untouched=True, policy_absent=True)
                finally:
                    backend.close()
                    plan.source.close()


def declare_control_contract():
    from support import control_semantics
    contract = remove_contract()
    groups = [
        ("test_exact_live_three_documents_and_fresh_suffix_fence", "PASS", ["remove:exact-live-three-documents", "remove-negative:fresh-private-suffix-fence", "remove-negative:locked-journal-promotion"]),
        ("test_direct_removal_pending_copy_catalogue", "PASS", ["remove-direct-pending-copy:" + key for key in PENDING_COPY_REMOVE_STATES]),
        ("test_complete_revoke_first_zero_residue_and_permanent_audit", "PASS", ["remove:revoke-first-zero-residue-audit"]),
        ("test_each_remove_phase_identity_and_no_install_republication", "PASS", ["remove-phase:" + row for row in contract["matrices"]["remove-phase-identity-no-republish"]]),
        ("test_policy_substitution_prevents_every_other_removal", "PASS", ["remove-negative:policy-substitution-" + key for key in POLICY_SUBSTITUTIONS]),
        ("test_active_execution_revoke_then_fence_and_resume", "PASS", ["remove-negative:active-execution-fence"]),
        ("test_substituted_payload_foreign_residue_and_live_grant", "PASS", ["remove-negative:" + key for key in
            ("payload-inode", "payload-mount", "extra-snapshot", "extra-stage", "foreign-live-grant")]),
    ]
    owners = {"remove-direct-pending-copy": __name__ + ".UninstallTests.test_direct_removal_pending_copy_catalogue",
        "remove-phase-identity-no-republish": __name__ + ".UninstallTests.test_each_remove_phase_identity_and_no_install_republication",
        "remove-policy-substitution": __name__ + ".UninstallTests.test_policy_substitution_prevents_every_other_removal"}
    for name, rows in contract["matrices"].items():
        if ":batch" in name:
            method = "test_" + name.replace("-", "_").replace(":", "_")
            groups.append((method, "PASS", ["remove-prefix:" + row for row in rows]))
            owners[name] = __name__ + ".UninstallTests." + method
    return dict(contract, observation_semantics=control_semantics(__name__, "UninstallTests", groups), matrix_owners=owners)


def _run_remove_prefix_batch(self,name,rows):
    plan,original,_=fixture("remove-prefix-batch"); root=original.capability.root; original.close()
    covered=[]
    try:
        with MemorySyscallReplay(root) as replay:
            backend=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN))
            try:
                Installer(plan,backend).install(); backend=removal_backend(plan,backend)
                snapshots,trace=remove_prefixes(plan,backend,replay)
            finally: backend.close()
            points={_fault_signature(point,plan):point for point in snapshots}
            evidence=retain_prefixes(root,[points[row] for row in rows],snapshots,trace)
            for row in rows:
                snapshot=snapshots[points[row]]; replay.restore(snapshot)
                seed=RecordingBackend(plan.capability(root,fake_root=True,test_token=TEST_TOKEN)); seed.import_state(snapshot["metadata"])
                backend=removal_backend(plan,seed)
                try:
                    self.assertEqual(recover_install(plan,backend)["phase"],"removed")
                    self.assertFalse(any(effect=="publish" for effect,path in backend.events))
                    covered.append(row); observe("remove-prefix",row,"PASS",actual_source_methods=True,
                        prefix_evidence=evidence,hook_index=snapshot["hook_index"],modeled_crash=True,
                        native_or_thrown_crash=False,policy_republished=False)
                finally: backend.close()
            matrix(name,rows,covered)
    finally: plan.source.close()


def _remove_prefix_batch_method(name,rows):
    def test(self): _run_remove_prefix_batch(self,name,rows)
    test.__name__="test_"+name.replace("-","_").replace(":","_")
    return test


for _matrix_name,_rows in remove_contract()["matrices"].items():
    if ":batch" in _matrix_name:
        _method=_remove_prefix_batch_method(_matrix_name,_rows)
        setattr(UninstallTests,_method.__name__,_method)
