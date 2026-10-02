import os
import stat
import unittest
from unittest import mock

from support import retained_root, observe, matrix, control_semantics
import ledger
from canonical import ContractError


def authority():
    return {"candidate_commit": "a" * 40, "candidate_tree": "b" * 40, "attempt_generation": 1,
            "package_index_sha256": "c" * 64, "snapshot_manifest_sha256": "d" * 64,
            "broker_bundle_sha256": "e" * 64}


class Crash(BaseException):
    pass


def declare_control_contract():
    return {"matrices": {"ledger-durability": sorted(ledger.LEDGER_FAULTS)},
        "required_observations": sorted(["ledger:permanent-recovery-token"] + ["ledger-fault:" + point for point in ledger.LEDGER_FAULTS]),
        "observation_semantics": control_semantics(__name__, "LedgerTests", [
            ("test_positive_permanent_and_recovery_only_token", "PASS", ["ledger:permanent-recovery-token"]),
            ("test_exhaustive_durability_prefix_faults", "PASS", ["ledger-fault:" + point for point in ledger.LEDGER_FAULTS]),
            ("test_existing_regular_and_modeled_hostile_nodes", "PASS", ["ledger-negative:" + key for key in ("valid", "empty", "torn", "corrupt", "directory", "symlink", "FIFO", "hardlink", "mount-overlay", "socket", "device")]),
            ("test_parent_and_record_identity_environment_negatives", "PASS", ["ledger-identity:" + key for key in ("file-swap", "mount-swap", "parent-swap", "byte-drift", "link-count", "mode-drift")]
                + ["ledger-capability:" + key for key in ("truncate", "unlink", "rename", "reset", "recreate", "rollback", "copy-from-backup", "reinstall", "uninstall", "new-manifest")]),
        ]), "matrix_owners": {"ledger-durability": __name__ + ".LedgerTests.test_exhaustive_durability_prefix_faults"}}


class LedgerTests(unittest.TestCase):
    def open_root(self, name):
        root = retained_root(name)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(os.close, fd)
        return root, fd

    def consume(self, fd, **kwargs):
        return ledger.consume_once(fd, authority(), authority_sha256="1" * 64, live_grant_sha256="2" * 64,
                                   owner_uid=os.getuid(), owner_gid=os.getgid(), **kwargs)

    def test_positive_permanent_and_recovery_only_token(self):
        root, fd = self.open_root("ledger-positive")
        token = self.consume(fd)
        self.addCleanup(token.close)
        self.assertTrue(token.validate())
        self.assertTrue(token.fresh)
        before = (root / "consumed.v1").read_bytes()
        with self.assertRaises(ContractError):
            self.consume(fd)
        recovered = ledger.load_consumed_for_recovery(fd, authority(), authority_sha256="1" * 64,
            live_grant_sha256="2" * 64, owner_uid=os.getuid(), owner_gid=os.getgid())
        self.addCleanup(recovered.close)
        self.assertFalse(recovered.fresh)
        self.assertEqual((root / "consumed.v1").read_bytes(), before)
        for name in ("reset", "retry", "delete", "repair", "reuse"):
            self.assertFalse(hasattr(ledger, name))
        observe("ledger", "permanent-recovery-token", "PASS", effects=list(ledger.LEDGER_EFFECTS))

    def test_exhaustive_durability_prefix_faults(self):
        seen = []
        for point in ledger.LEDGER_FAULTS:
            with self.subTest(point=point):
                root, fd = self.open_root("ledger-fault")
                reached = []
                def crash(actual):
                    reached.append(actual)
                    if actual == point:
                        raise Crash(point)
                with self.assertRaises(Crash):
                    self.consume(fd, fault=crash)
                self.assertIn(point, reached)
                exists = (root / "consumed.v1").exists()
                if point == "pre:ledger:exclusive_create":
                    self.assertFalse(exists)
                else:
                    self.assertTrue(exists)
                    before = (root / "consumed.v1").read_bytes()
                    with self.assertRaises(ContractError):
                        self.consume(fd)
                    self.assertEqual((root / "consumed.v1").read_bytes(), before)
                seen.append(point)
                observe("ledger-fault", point, "PASS", marker_present=exists, live_effects=0)
        matrix("ledger-durability", ledger.LEDGER_FAULTS, seen)

    def test_existing_regular_and_modeled_hostile_nodes(self):
        for name, raw in (("valid", b"{}\n"), ("empty", b""), ("torn", b"{"), ("corrupt", b"garbage")):
            with self.subTest(name=name):
                root, fd = self.open_root("ledger-existing")
                node = os.open("consumed.v1", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=fd)
                os.write(node, raw)
                os.close(node)
                with self.assertRaises(ContractError):
                    self.consume(fd)
                self.assertEqual((root / "consumed.v1").read_bytes(), raw)
                observe("ledger-negative", name, "PASS", unchanged=True)
        root, fd = self.open_root("ledger-directory")
        (root / "consumed.v1").mkdir(mode=0o700)
        with self.assertRaises(ContractError):
            self.consume(fd)
        observe("ledger-negative", "directory", "PASS")
        for kind in ("symlink", "FIFO", "hardlink", "mount-overlay", "socket", "device"):
            root, fd = self.open_root("ledger-modeled")
            native = os.open
            def modeled(path, flags, *args, **kwargs):
                if path == "consumed.v1" and flags & os.O_EXCL:
                    raise FileExistsError(kind)
                return native(path, flags, *args, **kwargs)
            with mock.patch.object(ledger.os, "open", side_effect=modeled):
                with self.assertRaises(ContractError):
                    self.consume(fd)
            self.assertEqual(list(root.iterdir()), [])
            observe("ledger-negative", kind, "PASS", modeled=True, host_specials=0)

    def test_parent_and_record_identity_environment_negatives(self):
        root, fd = self.open_root("ledger-identity")
        with self.assertRaises(ContractError):
            ledger.consume_once(fd, authority(), authority_sha256="1" * 64, live_grant_sha256="2" * 64)
        token = self.consume(fd)
        self.addCleanup(token.close)
        for kind in ("file-swap", "mount-swap", "parent-swap", "byte-drift", "link-count", "mode-drift"):
            with self.subTest(kind=kind):
                if kind == "mount-swap":
                    with mock.patch.object(ledger, "descriptor_mount", return_value=token.mount + 1):
                        with self.assertRaises(ContractError):
                            token.validate()
                elif kind == "parent-swap":
                    with mock.patch.object(ledger, "validate_parent", return_value=((0,) * 6, token.mount)):
                        with self.assertRaises(ContractError):
                            token.validate()
                elif kind == "byte-drift":
                    with mock.patch.object(ledger.os, "read", return_value=b"{}\n"):
                        with self.assertRaises(ContractError):
                            token.validate()
                else:
                    native = os.stat
                    original = native("consumed.v1", dir_fd=fd, follow_symlinks=False)
                    values = list(original)
                    values[{"file-swap": 1, "link-count": 3, "mode-drift": 0}[kind]] += 1
                    fake = os.stat_result(values)
                    def substitute(path, *args, **kwargs):
                        return fake if path == "consumed.v1" else native(path, *args, **kwargs)
                    with mock.patch.object(ledger.os, "stat", side_effect=substitute):
                        with self.assertRaises(ContractError):
                            token.validate()
                observe("ledger-identity", kind, "PASS")
        original = (root / "consumed.v1").read_bytes()
        for action in ("truncate", "unlink", "rename", "reset", "recreate", "rollback", "copy-from-backup", "reinstall", "uninstall", "new-manifest"):
            # An ordinary caller has no root-parent capability, even with an old
            # candidate/generation or different snapshot identity.
            with self.assertRaises(ContractError):
                ledger.consume_once(fd, authority(), authority_sha256="1" * 64, live_grant_sha256="2" * 64)
            self.assertEqual((root / "consumed.v1").read_bytes(), original)
            observe("ledger-capability", action, "PASS", caller_root_capability=False, bytes_unchanged=True)
