import dataclasses
import hashlib
import os
import sys
import unittest
from unittest import mock

from support import retained_root, observe, matrix, PACKAGE, control_semantics
from canonical import ContractError, canonical_bytes, digest
import broker_bootstrap as bootstrap
import broker_runtime as runtime
import ledger


class Crash(BaseException):
    pass


class RecordingPublisher:
    """Single-writer isolated-root publication; no privileged syscalls/links."""
    def publish(self, fd, source, target):
        try:
            os.stat(target, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(target)
        os.rename(source, target, src_dir_fd=fd, dst_dir_fd=fd)


def contracts():
    commit, tree, package, snapshot = "a" * 40, "b" * 40, "c" * 64, "d" * 64
    authority = {key: "e" * 64 for key in runtime.AUTHORITY_KEYS if key.endswith("_sha256")}
    authority.update(schema="installed-authority.v1", authority_id="synthetic", candidate_commit=commit,
        candidate_tree=tree, attempt_generation=1, package_index_sha256=package, snapshot_manifest_sha256=snapshot,
        bootstrap_python_path="/usr/bin/python3.14", snapshot_root="/usr/libexec/friday/quality-gate-toolchain-v1/" + snapshot,
        caller_uid=1000, gate_uid=1001, gate_gid=1001, sudoers_path="/etc/sudoers.d/friday-quality-gate-synthetic",
        ledger_directory="/var/lib/friday/quality-gate-v1/attempts/" + commit + "/1",
        live_lock_path="/var/lib/friday/quality-gate-v1/live.lock", scratch_parent="/var/lib/friday/quality-gate-v1/scratch",
        evidence_parent="/var/lib/friday/quality-gate-v1/evidence")
    authority["runtime_journal_path"] = authority["ledger_directory"] + "/runtime.v1.json"
    argv = list(runtime.FIXED_ARGV_PREFIX) + ["--synthetic-reviewed"]
    environment = dict(runtime.FIXED_ENVIRONMENT)
    authority["gate_argv_sha256"], authority["environment_sha256"] = digest(argv), digest(environment)
    startup = {"interpreter": argv[0], "prefix": "/opt/friday/quality-toolchain/venv",
        "node": "/opt/friday/quality-toolchain/bin/node", "unrar": "/opt/friday/quality-toolchain/bin/unrar",
        "browser": "/opt/friday/quality-toolchain/browsers/chromium", "headless_browser": "/opt/friday/quality-toolchain/browsers/headless",
        "driver": "/opt/friday/quality-toolchain/venv/lib/playwright/driver/node", "forbidden_startup_absent": True,
        "descendants": {name: argv[0] for name in ("build_backend", "wheel_verifier", "pip", "pytest", "xdist", "execnet")}}
    grant = {"schema": "live-grant.v1", "authority_id": "synthetic", "authority_sha256": digest(authority),
        "attempt_id": commit + "-1", "candidate_commit": commit, "attempt_generation": 1,
        "snapshot_manifest_sha256": snapshot, "rootfs_sha256": "1" * 64, "golden_sha256": "2" * 64,
        "gate_argv": argv, "environment": environment, "capacity": {"cpus": [0, 1], "memory_min_bytes": 1,
        "worker_count": 1, "cpu_max": "max 100000", "memory_max": "max"}, "timeout_sec": 30,
        "output_limit_bytes": 65536, "evidence_allowlist": ["terminal.v1.json"], "startup_receipt": startup,
        "one_terminal_attempt": True}
    return authority, grant


class RecordingBackend:
    def __init__(self, negative=None):
        self.events = []
        self.negative = negative
    def bind(self, consumed, authority, grant):
        consumed.validate()
        self.consumed, self.authority, self.grant = consumed, authority, grant
        self.events.append("bind-consumed")
    def step(self, name):
        self.consumed.validate()
        self.events.append(name)
        if self.negative == name:
            raise ContractError("modeled hostile " + name)
    def verify_snapshot(self):
        self.step("snapshot")
        return object()
    def seal_namespace(self, held):
        self.step("namespace")
    def admit(self):
        self.step("admission")
    def acquire_lock(self):
        self.step("lock")
    def start_child(self):
        self.step("exec")
        return 1
    def wait_child(self, child):
        self.step("wait")
        return 0
    def validate_evidence(self, status):
        self.step("evidence")
        return {"verdict": "PASS", "attempt_id": self.grant["attempt_id"]}
    def publish_evidence(self, evidence):
        self.step("publish")
        return digest(evidence)
    def abort(self):
        self.events.append("abort")
    def prepare_finalize(self):
        self.step("prepare-finalize")
    def finalization_guard(self):
        self.step("finalization-guard")
    def close(self):
        if getattr(self, "closed", False):
            return
        self.closed = True
        self.step("close")


MAIN_NEGATIVES = ("trust_binding", "grant_pin", "running-interpreter", "running-bootstrap", "startup-lifetime", "external-trust-pin", "external-live-pin", "null-runtime-pin")
EXTRA_BOOTSTRAP_CONTROLS = ("bundle-forgery", "bundle-mutation", "index-mode-alias", "self-host-startup-refusal", "ancestor-lease")


def declare_control_contract():
    semantics = control_semantics(__name__, "BrokerTests", [
        ("test_recording_run_consumption_precedes_all_effects", "PASS", ["runtime:ordered-recording-positive"]),
        ("test_exhaustive_abrupt_runtime_prefix_recovery", "PASS", ["runtime-fault:" + point for point in runtime.RUNTIME_FAULTS]),
        ("test_identity_signal_timeout_evidence_publication_negatives", "PASS", ["runtime-negative:" + key for key in ("candidate-parent", "golden-parent", "scratch-parent", "evidence-parent", "rename-exchange", "proc-root-fd", "descriptor-injection", "mount-propagation", "same-device-bind", "signal-child", "signal-supervisor", "timeout", "extra-evidence", "evidence-substitution", "publication-race", "lock-loss", "capacity-drift")]),
        ("test_journal_torn_foreign_history_special_and_reboot_negatives", "PASS", ["runtime-journal-negative:" + key for key in ("torn-stage", "foreign-stage", "hash-invalid-stage", "history-gap", "old-attempt", "two-link-window", "reboot-mount-change")]),
        ("test_fixed_authority_environment_and_startup_negatives", "PASS", ["startup-negative:" + key for key in runtime.FORBIDDEN_ENVIRONMENT + ("sitecustomize", "usercustomize", ".pth", "editable-finder", "entry-point-plugin", "browser-download", "npm_config_proxy", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "build_backend", "wheel_verifier", "pip", "pytest", "xdist", "execnet", "node", "unrar", "browser", "headless_browser", "driver", "interpreter", "prefix", "forbidden_startup_absent")]
            + ["authority-negative:" + key for key in ("snapshot_root", "ledger_directory", "runtime_journal_path", "live_lock_path", "evidence_parent", "scratch_parent", "sudoers_path", "bootstrap_python_path")]),
        ("test_transport_and_external_closed_bundle", "PASS", ["bootstrap-negative:" + key for key in ("argv", "operation", "environment", "sys_path", "preload", "descriptor", "flags", "interpreter", "index_digest", "source_substitution-before_open", "source_substitution-after_read")]
            + ["bootstrap:exact-spec-positive-cleanup"]),
        ("test_bundle_exact_inventory_and_member_identity_negatives", "PASS", ["bootstrap-negative:" + key for key in ("index_extra", "index_missing", "source_digest", "source_owner", "source_mode", "source_links", "source_type", "source_mount", "self_digest")]),
        ("test_strict_trust_pin_and_numeric_alias_parsers", "PASS", ["bootstrap-negative:" + key for key in bootstrap.BOOTSTRAP_PIN_NEGATIVES]),
        ("test_actual_final_bundle_main_and_preload_refusal", "PASS", ["bootstrap-negative:" + key for key in MAIN_NEGATIVES + ("source_substitution",)]
            + ["bootstrap:actual-final-bundle-main", "bootstrap:transport-interruption-refusal"]),
        ("test_late_finalization_refuses_and_vetoes_committed_pass", "PASS", ["runtime:finalization-veto"]),
        ("test_loader_requires_issued_immutable_bundle_and_exact_index_modes", "PASS", ["bootstrap-negative:" + key for key in ("bundle-forgery", "bundle-mutation", "index-mode-alias", "self-host-startup-refusal")]),
        ("test_bootstrap_retains_ancestry_through_descriptor_lifetime", "PASS", ["bootstrap-negative:ancestor-lease"]),
    ])
    return {"matrices": {"runtime-abrupt-prefix": sorted(runtime.RUNTIME_FAULTS)},
        "required_observations": sorted(set("bootstrap-negative:" + key for key in bootstrap.BOOTSTRAP_NEGATIVES + bootstrap.BOOTSTRAP_PIN_NEGATIVES + MAIN_NEGATIVES + EXTRA_BOOTSTRAP_CONTROLS) | {
            "bootstrap:actual-final-bundle-main", "runtime:ordered-recording-positive", "runtime:finalization-veto",
            "bootstrap:transport-interruption-refusal"}),
        "observation_semantics": semantics,
        "matrix_owners": {"runtime-abrupt-prefix": __name__ + ".BrokerTests.test_exhaustive_abrupt_runtime_prefix_recovery"}}


class FullBundleRecordingIO:
    """Fresh bounded actual source bytes; all kernel/child behavior is recording."""
    def __init__(self, root, root_fd):
        self.bundle_path = str(root / "bundle")
        (root / "bundle").mkdir(mode=0o700)
        (root / "ledger").mkdir(mode=0o700)
        self.bundle_fd = os.open(root / "bundle", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.ledger_fd = os.open(root / "ledger", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        rows = []
        for name in bootstrap.BUNDLE_NAMES:
            raw = (PACKAGE / "src" / name).read_bytes()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=self.bundle_fd)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            rows.append({"path": name, "role": "runtime", "mode": 0o600, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        self.index = canonical_bytes({"schema": "friday.package-index.v1", "members": rows})
        fd = os.open("package-index.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.bundle_fd)
        os.write(fd, self.index)
        os.close(fd)
        self.authority, self.grant = contracts()
        self.authority["broker_bundle_sha256"] = hashlib.sha256(self.index).hexdigest()
        self.grant["authority_sha256"] = digest(self.authority)
        self.bill_raw = (PACKAGE / "effects/live-one-attempt.v1.json").read_bytes()
        self.trust = {"schema": "friday.bootstrap-trust.v1", "authority_id": self.authority["authority_id"],
            "authority_sha256": digest(self.authority), "package_index_sha256": self.authority["package_index_sha256"],
            "runtime_index_sha256": hashlib.sha256(self.index).hexdigest(), "bootstrap_python_sha256": self.authority["bootstrap_python_sha256"],
            "environment": {"LANG": "C"}, "sys_path": ["/usr/lib/python3.14"], "allowed_fds": [0, 1, 2]}
        self.pin = {"schema": "live-grant-pin.v1", "authority_sha256": digest(self.authority),
            "live_grant_sha256": digest(self.grant), "effect_bill_sha256": hashlib.sha256(self.bill_raw).hexdigest()}
        self.trust_sha256, self.live_pin_sha256 = digest(self.trust), digest(self.pin)
        self.capability = bootstrap.FixtureBootstrapCapability.create(root_fd)
        self.broker = "/usr/libexec/friday/quality-gate-broker-v1/" + self.trust["package_index_sha256"] + "/broker_bootstrap.py"
        identity = list(bootstrap._identity(os.stat(PACKAGE / "src/broker_bootstrap.py")))
        interpreter = [1, 2, 0o100755, 0, 0, 1, 100, 1, 1]
        self.startup = {"interpreter_opened": interpreter, "interpreter_named": interpreter, "interpreter_running": interpreter,
            "interpreter_sha256": self.trust["bootstrap_python_sha256"], "bootstrap_opened": identity,
            "bootstrap_named": identity, "bootstrap_running": identity,
            "bootstrap_sha256": hashlib.sha256((PACKAGE / "src/broker_bootstrap.py").read_bytes()).hexdigest(),
            "protected_lifetime": True, "authority_mode": "fixture"}
        self.publisher = RecordingPublisher()
        self.emitted = []
    def transport_state(self):
        return bootstrap.TransportState((self.broker, "run-v1"), self.trust["environment"], tuple(self.trust["sys_path"]), (), (0, 1, 2), "/usr/bin/python3.14", True, True, True)
    def read_trust(self):
        return canonical_bytes(self.trust)
    def read_pin(self):
        return canonical_bytes(self.pin)
    def read_index(self):
        return self.index
    def startup_identity(self):
        return self.startup
    def read_subjects(self):
        return canonical_bytes(self.authority), canonical_bytes(self.grant), self.bill_raw
    def backend(self, runtime_module, authority, grant):
        return RecordingBackend()
    def evidence_raw(self, backend):
        return canonical_bytes({"verdict": "PASS", "attempt_id": self.grant["attempt_id"]})
    def emit_result(self, raw):
        self.emitted.append(raw)
    def close(self):
        os.close(self.bundle_fd)
        os.close(self.ledger_fd)


class BrokerTests(unittest.TestCase):
    def root(self, name):
        root = retained_root(name)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(os.close, fd)
        return root, fd
    def run(self, *args, **kwargs):
        # Preserve unittest.TestCase.run; helper is execute, not a test runner.
        return super().run(*args, **kwargs)
    def execute(self, fd, authority, grant, backend, fault=None):
        return runtime.run_once(authority, grant, authority_sha256=digest(authority), live_grant_sha256=digest(grant),
            ledger_dir_fd=fd, backend=backend, owner_uid=os.getuid(), owner_gid=os.getgid(), fault=fault,
            journal_publisher=RecordingPublisher())
    def recovery(self, fd, authority, grant):
        token = ledger.load_consumed_for_recovery(fd, authority, authority_sha256=digest(authority),
            live_grant_sha256=digest(grant), owner_uid=os.getuid(), owner_gid=os.getgid())
        journal = runtime.RuntimeJournal(fd, token, owner_uid=os.getuid(), owner_gid=os.getgid(), publisher=RecordingPublisher())
        try:
            return runtime.recover_runtime(journal)
        finally:
            journal.close()
            token.close()

    def test_recording_run_consumption_precedes_all_effects(self):
        root, fd = self.root("broker-positive")
        authority, grant = contracts()
        backend = RecordingBackend()
        result = self.execute(fd, authority, grant, backend)
        self.assertEqual(result["phase"], "terminal_pass")
        self.assertTrue((root / "terminal-commit.v1.json").exists())
        before = (root / "consumed.v1").read_bytes()
        with self.assertRaises(ContractError):
            self.execute(fd, authority, grant, RecordingBackend())
        self.assertEqual((root / "consumed.v1").read_bytes(), before)
        self.assertEqual(backend.events[:9], ["bind-consumed", "snapshot", "namespace", "admission", "lock", "exec", "wait", "evidence", "publish"])
        token = ledger.load_consumed_for_recovery(fd, authority, authority_sha256=digest(authority), live_grant_sha256=digest(grant),
            owner_uid=os.getuid(), owner_gid=os.getgid())
        journal = runtime.RuntimeJournal(fd, token, owner_uid=os.getuid(), owner_gid=os.getgid(), publisher=RecordingPublisher())
        try:
            evidence_raw = canonical_bytes({"verdict": "PASS", "attempt_id": grant["attempt_id"]})
            self.assertTrue(runtime.validate_terminal_commit(journal, evidence_raw, result["journal_sha256"]))
            self.assertTrue(runtime.validate_supervisor_result(journal, evidence_raw, result["supervisor_result"]))
            forged = dataclasses.replace(result["supervisor_result"])
            with self.assertRaises(ContractError):
                runtime.validate_supervisor_result(journal, evidence_raw, forged)
            unfinished = dataclasses.replace(result["supervisor_result"], finalization_complete=False)
            with self.assertRaises(ContractError):
                runtime.validate_supervisor_result(journal, evidence_raw, unfinished)
            with self.assertRaises(ContractError):
                runtime.validate_terminal_commit(journal, b"{}\n", result["journal_sha256"])
            with self.assertRaises(ContractError):
                runtime.validate_terminal_commit(journal, evidence_raw, "f" * 64)
            journal.failure_marker("test ambiguity after provisional publication")
            with self.assertRaises(ContractError):
                runtime.validate_terminal_commit(journal, evidence_raw, result["journal_sha256"])
        finally:
            journal.close()
            token.close()
        observe("runtime", "ordered-recording-positive", "PASS", events=backend.events, production_live=False,
            exact_same_invocation_result_issued=True, forged_copy_refused=True, unfinished_copy_refused=True,
            arbitrary_in_process_mutation_proof=False)

    def test_exhaustive_abrupt_runtime_prefix_recovery(self):
        seen = []
        for point in runtime.RUNTIME_FAULTS:
            with self.subTest(point=point):
                root, fd = self.root("runtime-prefix")
                authority, grant = contracts()
                reached = []
                def crash(actual):
                    reached.append(actual)
                    if actual == point:
                        raise Crash(point)
                if point.endswith(":terminal_fail"):
                    token = ledger.consume_once(fd, authority, authority_sha256=digest(authority), live_grant_sha256=digest(grant),
                        owner_uid=os.getuid(), owner_gid=os.getgid())
                    journal = runtime.RuntimeJournal(fd, token, owner_uid=os.getuid(), owner_gid=os.getgid(), publisher=RecordingPublisher())
                    journal.advance("consumed")
                    journal.fault = crash
                    try:
                        with self.assertRaises(Crash):
                            journal.advance("terminal_fail", outcome="FAIL")
                    finally:
                        journal.close()
                        token.close()
                else:
                    with self.assertRaises(Crash):
                        self.execute(fd, authority, grant, RecordingBackend(), crash)
                self.assertIn(point, reached)
                before = (root / "consumed.v1").read_bytes()
                recovered = self.recovery(fd, authority, grant)
                self.assertEqual(recovered["phase"], "terminal_fail")
                self.assertEqual((root / "consumed.v1").read_bytes(), before)
                with self.assertRaises(ContractError):
                    self.execute(fd, authority, grant, RecordingBackend())
                seen.append(point)
                observe("runtime-fault", point, "PASS", recovery="terminal_fail", consumed_unchanged=True,
                        abrupt_prefix=True, durable_commit_present=(root / "terminal-commit.v1.json").exists())
        matrix("runtime-abrupt-prefix", runtime.RUNTIME_FAULTS, seen)

    def test_identity_signal_timeout_evidence_publication_negatives(self):
        bindings = {"candidate-parent": "snapshot", "golden-parent": "snapshot", "scratch-parent": "namespace",
            "evidence-parent": "evidence", "rename-exchange": "snapshot", "proc-root-fd": "namespace", "descriptor-injection": "namespace",
            "mount-propagation": "namespace", "same-device-bind": "namespace", "signal-child": "wait", "signal-supervisor": "wait",
            "timeout": "wait", "extra-evidence": "evidence", "evidence-substitution": "evidence", "publication-race": "publish",
            "lock-loss": "lock", "capacity-drift": "admission"}
        for name, operation in bindings.items():
            with self.subTest(name=name):
                root, fd = self.root("runtime-negative")
                authority, grant = contracts()
                backend = RecordingBackend(operation)
                result = self.execute(fd, authority, grant, backend)
                self.assertEqual(result["phase"], "terminal_fail")
                self.assertFalse((root / "terminal-commit.v1.json").exists())
                self.assertTrue((root / "consumed.v1").exists())
                observe("runtime-negative", name, "PASS", modeled=True, production_security_proof=False)

    def test_journal_torn_foreign_history_special_and_reboot_negatives(self):
        for name in ("torn-stage", "foreign-stage", "hash-invalid-stage", "history-gap", "old-attempt", "two-link-window", "reboot-mount-change"):
            with self.subTest(name=name):
                root, fd = self.root("runtime-journal-negative")
                authority, grant = contracts()
                token = ledger.consume_once(fd, authority, authority_sha256=digest(authority), live_grant_sha256=digest(grant),
                    owner_uid=os.getuid(), owner_gid=os.getgid())
                journal = runtime.RuntimeJournal(fd, token, owner_uid=os.getuid(), owner_gid=os.getgid(), publisher=RecordingPublisher())
                try:
                    journal.advance("consumed")
                    if name == "history-gap":
                        current = journal.advance("snapshot_verifying")
                        changed = {**current, "previous_journal_sha256": "f" * 64}
                        changed["journal_sha256"] = digest({key: value for key, value in changed.items() if key != "journal_sha256"})
                        os.rename("runtime.v1.json", "runtime.saved", src_dir_fd=fd, dst_dir_fd=fd)
                        node = os.open("runtime.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=fd)
                        os.write(node, canonical_bytes(changed))
                        os.close(node)
                    elif name not in ("two-link-window", "reboot-mount-change"):
                        def stage_crash(point):
                            if point == "post:journal_stage:snapshot_verifying":
                                raise Crash(point)
                        journal.fault = stage_crash
                        with self.assertRaises(Crash):
                            journal.advance("snapshot_verifying")
                        journal.fault = None
                        staged, unused = journal._read(".runtime.v1.json.new")
                        os.rename(".runtime.v1.json.new", "stage.saved", src_dir_fd=fd, dst_dir_fd=fd)
                        if name == "torn-stage":
                            raw = b"{"
                        else:
                            changed = {**staged}
                            if name == "foreign-stage":
                                changed["identity_sha256"] = "f" * 64
                            elif name == "old-attempt":
                                changed["attempt_id"] = "f" * 40 + "-1"
                            else:
                                changed["journal_sha256"] = "f" * 64
                            if name != "hash-invalid-stage":
                                changed["journal_sha256"] = digest({key: value for key, value in changed.items() if key != "journal_sha256"})
                            raw = canonical_bytes(changed)
                        node = os.open(".runtime.v1.json.new", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=fd)
                        os.write(node, raw)
                        os.close(node)
                    journal.fault = None
                    if name == "two-link-window":
                        native = os.stat
                        def two_links(path, *args, **kwargs):
                            info = native(path, *args, **kwargs)
                            if path == "runtime.v1.json":
                                values = list(info)
                                values[3] = 2
                                return os.stat_result(values)
                            return info
                        with mock.patch.object(runtime.os, "stat", side_effect=two_links):
                            result = runtime.recover_runtime(journal)
                    elif name == "reboot-mount-change":
                        with mock.patch.object(ledger, "descriptor_mount", return_value=token.mount + 1):
                            result = runtime.recover_runtime(journal)
                    else:
                        result = runtime.recover_runtime(journal)
                    self.assertEqual(result["phase"], "terminal_fail")
                    self.assertTrue((root / "consumed.v1").exists())
                    observe("runtime-journal-negative", name, "PASS", recovery="terminal_fail", permanent_ledger=True)
                finally:
                    journal.close()
                    token.close()

    def test_fixed_authority_environment_and_startup_negatives(self):
        authority, grant = contracts()
        runtime.parse_installed_authority(canonical_bytes(authority), digest(authority))
        runtime.parse_live_grant(canonical_bytes(grant), digest(grant), authority, digest(authority))
        for name in runtime.FORBIDDEN_ENVIRONMENT + ("sitecustomize", "usercustomize", ".pth", "editable-finder", "entry-point-plugin", "browser-download", "npm_config_proxy", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
            changed = {**grant, "environment": {**grant["environment"], name: "injected"}}
            with self.assertRaises(ContractError):
                runtime.parse_live_grant(canonical_bytes(changed), digest(changed), authority, digest(authority))
            observe("startup-negative", name, "PASS", boundary="closed-environment")
        for name in ("build_backend", "wheel_verifier", "pip", "pytest", "xdist", "execnet"):
            receipt = {**grant["startup_receipt"], "descendants": {**grant["startup_receipt"]["descendants"], name: "/tmp/foreign/python"}}
            changed = {**grant, "startup_receipt": receipt}
            with self.assertRaises(ContractError):
                runtime.parse_live_grant(canonical_bytes(changed), digest(changed), authority, digest(authority))
            observe("startup-negative", name, "PASS", boundary="descendant-interpreter")
        for name in ("node", "unrar", "browser", "headless_browser", "driver", "interpreter", "prefix", "forbidden_startup_absent"):
            receipt = {**grant["startup_receipt"], name: False if name == "forbidden_startup_absent" else "/tmp/foreign"}
            changed = {**grant, "startup_receipt": receipt}
            with self.assertRaises(ContractError):
                runtime.parse_live_grant(canonical_bytes(changed), digest(changed), authority, digest(authority))
            observe("startup-negative", name, "PASS", boundary="fixed-startup-receipt")
        for field in ("snapshot_root", "ledger_directory", "runtime_journal_path", "live_lock_path", "evidence_parent", "scratch_parent", "sudoers_path", "bootstrap_python_path"):
            changed = {**authority, field: "/tmp/alternate"}
            with self.assertRaises(ContractError):
                runtime.parse_installed_authority(canonical_bytes(changed), digest(changed))
            observe("authority-negative", field, "PASS")

    def test_transport_and_external_closed_bundle(self):
        broker = "/usr/libexec/friday/quality-gate-broker-v1/" + "a" * 64 + "/broker_bootstrap.py"
        state = bootstrap.TransportState((broker, "run-v1"), {"LANG": "C"}, ("/usr/lib/python3.14",), (), (0, 1, 2), "/usr/bin/python3.14", True, True, True)
        bootstrap.verify_transport(state, broker, state.environment, state.sys_path, state.descriptors)
        cases = {"argv": {"argv": (broker, "run-v1", "/tmp/arbitrary")}, "operation": {"argv": (broker, "reset")},
            "environment": {"environment": {"LANG": "C", "PYTHONPATH": "/tmp"}}, "sys_path": {"sys_path": ("/tmp",)},
            "preload": {"modules": ("broker_runtime",)}, "descriptor": {"descriptors": (0, 1, 2, 99)},
            "flags": {"isolated": False}, "interpreter": {"interpreter": "/tmp/python"}}
        for name, change in cases.items():
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.verify_transport(dataclasses.replace(state, **change), broker, state.environment, state.sys_path, state.descriptors)
            observe("bootstrap-negative", name, "PASS")
        root, fd = self.root("bootstrap-bundle")
        rows = []
        for name in bootstrap.BUNDLE_NAMES:
            raw = b"VALUE=1\n"
            node = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            os.write(node, raw)
            os.close(node)
            rows.append({"path": name, "role": "runtime", "mode": 0o600, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        index = {"schema": "friday.package-index.v1", "members": rows}
        index_raw = canonical_bytes(index)
        node = os.open("package-index.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        os.write(node, index_raw)
        os.close(node)
        bundle = bootstrap.authenticate_bundle(fd, index_raw, digest(index), owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600, fixed_bundle_path=str(root))
        saved = {name: sys.modules.pop(name) for name in bootstrap.MODULE_ORDER if name in sys.modules}
        try:
            with bootstrap.exact_spec_load(bundle) as loaded:
                self.assertEqual(loaded.VALUE, 1)
            self.assertFalse(any(name in sys.modules for name in bootstrap.MODULE_ORDER))
        finally:
            sys.modules.update(saved)
        observe("bootstrap", "exact-spec-positive-cleanup", "PASS")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.authenticate_bundle(fd, index_raw, "f" * 64, owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600)
        observe("bootstrap-negative", "index_digest", "PASS")
        for point in ("before_open", "after_read"):
            changed = False
            def swap(actual, name):
                nonlocal changed
                if actual == point and not changed and name == "canonical.py":
                    changed = True
                    os.rename("canonical.py", "canonical.saved", src_dir_fd=fd, dst_dir_fd=fd)
                    node = os.open("canonical.py", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                    os.write(node, b"VALUE=1\n")
                    os.close(node)
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.stable_read(fd, "canonical.py", owner_uid=os.getuid(), owner_gid=os.getgid(), mode=0o600, fault=swap)
            os.rename("canonical.py", "canonical.replaced." + point, src_dir_fd=fd, dst_dir_fd=fd)
            os.rename("canonical.saved", "canonical.py", src_dir_fd=fd, dst_dir_fd=fd)
            observe("bootstrap-negative", "source_substitution-" + point, "PASS")

    def test_bundle_exact_inventory_and_member_identity_negatives(self):
        cases = ("index_extra", "index_missing", "source_digest", "source_owner", "source_mode", "source_links", "source_type", "source_mount", "self_digest")
        for case in cases:
            with self.subTest(case=case):
                root, fd = self.root("bootstrap-negative-bundle")
                rows = []
                for name in bootstrap.BUNDLE_NAMES:
                    raw = b"VALUE=1\n"
                    node = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                    os.write(node, raw)
                    os.close(node)
                    rows.append({"path": name, "role": "runtime", "mode": 0o600, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
                if case == "index_extra":
                    rows.append({"path": "foreign.py", "role": "runtime", "mode": 0o600, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
                elif case == "index_missing":
                    rows.pop()
                index = {"schema": "friday.package-index.v1", "members": rows}
                index_raw = canonical_bytes(index)
                node = os.open("package-index.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                os.write(node, index_raw)
                os.close(node)
                if case in ("source_digest", "self_digest"):
                    name = "canonical.py" if case == "source_digest" else "broker_bootstrap.py"
                    os.rename(name, "saved-source", src_dir_fd=fd, dst_dir_fd=fd)
                    node = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                    os.write(node, b"VALUE=2\n")
                    os.close(node)
                native_stat = os.stat
                def modeled_stat(path, *args, **kwargs):
                    info = native_stat(path, *args, **kwargs)
                    if path == "canonical.py" and case in ("source_owner", "source_mode", "source_links", "source_type"):
                        values = list(info)
                        if case == "source_owner":
                            values[4] += 1
                        elif case == "source_mode":
                            values[0] |= 0o020
                        elif case == "source_links":
                            values[3] = 2
                        else:
                            values[0] = 0o010600  # Modeled FIFO, never host mkfifo.
                        return os.stat_result(values)
                    return info
                mount = bootstrap._mount(fd)
                native_mount = bootstrap._mount
                def modeled_mount(value):
                    return mount if value == fd else mount + 1
                with mock.patch.object(bootstrap.os, "stat", side_effect=modeled_stat), \
                     mock.patch.object(bootstrap, "_mount", side_effect=modeled_mount if case == "source_mount" else native_mount):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.authenticate_bundle(fd, index_raw, digest(index), owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600)
                observe("bootstrap-negative", case, "PASS", modeled_specials=case == "source_type")

    def test_strict_trust_pin_and_numeric_alias_parsers(self):
        root, fd = self.root("bootstrap-strict-documents")
        recording = FullBundleRecordingIO(root, fd)
        try:
            for document, fields, parser, prefix in ((recording.trust, bootstrap.BOOTSTRAP_PIN_FIELDS, bootstrap.parse_bootstrap_trust, "trust"),
                    (recording.pin, sorted(bootstrap.PIN_KEYS - {"schema"}), bootstrap.parse_live_pin, "live-pin")):
                for field in fields:
                    for kind, value in (("absent", None), ("null", None), ("boolean", False), ("integer", 0), ("wrong", "bad")):
                        changed = {**document}
                        if kind == "absent":
                            changed.pop(field)
                        else:
                            changed[field] = value
                        with self.assertRaises(bootstrap.BootstrapError):
                            parser(canonical_bytes(changed), digest(changed))
                        observe("bootstrap-negative", prefix + "-" + field + "-" + kind, "PASS", package_modules_executed=0)
                for pin in (None, False, 0, "bad", "f" * 64):
                    with self.assertRaises(bootstrap.BootstrapError):
                        parser(canonical_bytes(document), pin)
            for alias in ([False, True, 2], [0.0, 1.0, 2.0]):
                changed = {**recording.trust, "allowed_fds": alias}
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.parse_bootstrap_trust(canonical_bytes(changed), digest(changed))
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.verify_transport(dataclasses.replace(recording.transport_state(), descriptors=tuple(alias)), recording.broker,
                        recording.trust["environment"], recording.trust["sys_path"], alias)
            for parser, document in ((runtime.parse_installed_authority, recording.authority),):
                for pin in (None, False, 0, "bad"):
                    with self.assertRaises(ContractError):
                        parser(canonical_bytes(document), pin)
            for pin in (None, False, 0, "bad"):
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.authenticate_bundle(recording.bundle_fd, recording.index, pin,
                        owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600)
        finally:
            recording.close()

    def test_actual_final_bundle_main_and_preload_refusal(self):
        for case in ("positive",) + MAIN_NEGATIVES:
            with self.subTest(case=case):
                root, fd = self.root("bootstrap-full-main")
                recording = FullBundleRecordingIO(root, fd)
                saved = {name: sys.modules.pop(name) for name in bootstrap.MODULE_ORDER if name in sys.modules}
                try:
                    if case == "trust_binding":
                        recording.trust["package_index_sha256"] = "f" * 64
                        recording.trust_sha256 = digest(recording.trust)
                    elif case == "grant_pin":
                        recording.pin["authority_sha256"] = "f" * 64
                        recording.live_pin_sha256 = digest(recording.pin)
                    elif case.startswith("running-"):
                        name = case[len("running-"):] + "_running"
                        recording.startup[name] = [*recording.startup[name]]
                        recording.startup[name][1] += 1
                    elif case == "startup-lifetime":
                        recording.startup["protected_lifetime"] = False
                    elif case == "external-trust-pin":
                        recording.trust_sha256 = None
                    elif case == "external-live-pin":
                        recording.live_pin_sha256 = None
                    elif case == "null-runtime-pin":
                        recording.trust["runtime_index_sha256"] = None
                        recording.trust_sha256 = digest(recording.trust)
                    with mock.patch.object(bootstrap, "exact_spec_load", wraps=bootstrap.exact_spec_load) as loader:
                        if case == "positive":
                            self.assertEqual(bootstrap.main(recording), 0)
                            self.assertEqual(loader.call_count, 1)
                            self.assertEqual(len(recording.emitted), 1)
                            receipt = __import__("json").loads(recording.emitted[0])
                            self.assertTrue(bootstrap.validate_result_transport(recording.emitted[0], receipt["transport"], invocation_complete=True, exit_code=0))
                            for complete, rc in ((False, 0), (True, 1), (True, False)):
                                with self.assertRaises(bootstrap.BootstrapError):
                                    bootstrap.validate_result_transport(recording.emitted[0], receipt["transport"], invocation_complete=complete, exit_code=rc)
                            changed = {**receipt, "schema": "friday.supervisor-transport.v0"}
                            with self.assertRaises(bootstrap.BootstrapError):
                                bootstrap.validate_result_transport(canonical_bytes(changed), receipt["transport"], invocation_complete=True, exit_code=0)
                            observe("bootstrap", "actual-final-bundle-main", "PASS", actual_bundle_names=list(bootstrap.BUNDLE_NAMES), actual_loader=True,
                                authenticated_startup_mode="fixture", production_startup_proof=False, kernel_child_proof=False, read_only_acceptance=True)
                            observe("bootstrap", "transport-interruption-refusal", "PASS", requires_external_invocation_authentication=True)
                        else:
                            with self.assertRaises(bootstrap.BootstrapError):
                                bootstrap.main(recording)
                            self.assertEqual(loader.call_count, 0)
                            self.assertEqual(recording.emitted, [])
                            observe("bootstrap-negative", case, "PASS", actual_main=True, package_modules_executed=0)
                finally:
                    sys.modules.update(saved)
                    recording.close()
        observe("bootstrap-negative", "source_substitution", "PASS", evidence="source_substitution-before_open/after_read")

    def test_late_finalization_refuses_and_vetoes_committed_pass(self):
        for operation in ("prepare-finalize", "finalization-guard", "close"):
            root, fd = self.root("runtime-finalization-veto")
            authority, grant = contracts()
            backend = RecordingBackend(operation)
            result = self.execute(fd, authority, grant, backend)
            self.assertEqual(result["phase"], "terminal_fail")
            self.assertNotIn("supervisor_result", result)
            if operation == "close":
                self.assertTrue((root / "terminal-commit.v1.json").exists())
                self.assertTrue((root / "terminal-failure.v1.json").exists())
        observe("runtime", "finalization-veto", "PASS", commit_before_fence_release=True, interrupted_result_refused=True, kernel_proof=False)

    def test_loader_requires_issued_immutable_bundle_and_exact_index_modes(self):
        root, fd = self.root("bootstrap-issued-bundle")
        recording = FullBundleRecordingIO(root, fd)
        try:
            bundle = bootstrap.authenticate_bundle(recording.bundle_fd, recording.index, hashlib.sha256(recording.index).hexdigest(),
                owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600)
            with self.assertRaises(TypeError):
                bundle.source["canonical.py"] = b"raise RuntimeError('foreign')\n"
            forged = bootstrap.AuthenticatedBundle(bundle.index_sha256, bundle.source, bundle.root_identity, bundle.root_mount, bundle.path)
            with self.assertRaises(bootstrap.BootstrapError):
                with bootstrap.exact_spec_load(forged):
                    self.fail("unissued bundle executed")
            index = __import__("json").loads(recording.index)
            for alias in (float(0o600), False):
                changed = {**index, "members": [{**row, "mode": alias} if number == 0 else row for number, row in enumerate(index["members"])]}
                raw = canonical_bytes(changed)
                with mock.patch.object(bootstrap, "stable_read", return_value=raw):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.authenticate_bundle(recording.bundle_fd, raw, digest(changed), owner_uid=os.getuid(), owner_gid=os.getgid(), source_mode=0o600)
            for key in ("bundle-forgery", "bundle-mutation", "index-mode-alias"):
                observe("bootstrap-negative", key, "PASS", package_modules_executed=0, actual_authenticator=True)
            with self.assertRaisesRegex(bootstrap.BootstrapError, "NOT_PROVEN"):
                bootstrap.verify_startup_identity({**recording.startup, "authority_mode": "host"}, bundle, recording.trust["bootstrap_python_sha256"], fixture=False)
            with mock.patch.object(bootstrap, "actual_transport", return_value=recording.transport_state()), \
                    mock.patch.object(bootstrap.os, "geteuid", return_value=0), mock.patch.object(bootstrap.os, "getegid", return_value=0), \
                    mock.patch.object(bootstrap.os, "open") as opened:
                with self.assertRaisesRegex(bootstrap.BootstrapError, "anchor NOT_PROVEN"):
                    bootstrap.main()
                opened.assert_not_called()
            observe("bootstrap-negative", "self-host-startup-refusal", "PASS", self_asserted_host_receipt_refused=True, package_modules_executed=0)
        finally:
            recording.close()

    def test_bootstrap_retains_ancestry_through_descriptor_lifetime(self):
        root, fd = self.root("bootstrap-ancestor-lease")
        (root / "a").mkdir(mode=0o700)
        (root / "a/b").mkdir(mode=0o700)
        lease = bootstrap._open_chain(fd, "/a/b", directory=True, owner_uid=os.getuid(), owner_gid=os.getgid())
        try:
            self.assertTrue(lease.check())
            self.assertEqual(len(lease.descriptors), 3)
            os.rename(root / "a", root / "saved-a")
            (root / "a").mkdir(mode=0o700)
            (root / "a/b").mkdir(mode=0o700)
            with self.assertRaises(bootstrap.BootstrapError):
                lease.check()
        finally:
            lease.close()
        mount = bootstrap._mount(fd)
        with mock.patch.object(bootstrap, "_mount", side_effect=lambda value: mount if value == fd else mount + value):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap._open_chain(fd, "/a/b", directory=True, owner_uid=os.getuid(), owner_gid=os.getgid())
        observe("bootstrap-negative", "ancestor-lease", "PASS", actual_descriptor_methods=True, rename_detachment_refused=True, modeled_mount_transition_refused=True)
