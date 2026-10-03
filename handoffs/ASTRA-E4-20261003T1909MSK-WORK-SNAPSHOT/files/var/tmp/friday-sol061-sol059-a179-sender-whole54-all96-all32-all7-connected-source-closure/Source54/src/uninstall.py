"""Revoke-first removal with exact identities and permanent audit preservation."""
from __future__ import annotations

import hashlib

from canonical import ContractError
from install import Installer, REMOVE_PHASES, INSTALL_PHASES, _parents
from install_bootstrap import (JOURNAL_PATH, JOURNAL_STAGE, INSTALL_DIRECTORY, INSTALL_LOCK,
                               LIVE_LOCK, NAMESPACE, PERMANENT_PREFIXES)


class Remover(Installer):
    def _phase(self, phase):
        if self.journal.value["phase"] != phase:
            self.journal.commit(phase)

    def _exact_remove(self, path):
        identity = self.backend.exists(path)
        expected = self.journal.value["objects"].get(path)
        if identity is None:
            if expected is not None:
                raise ContractError("journaled artifact vanished before exact removal")
            return False
        if expected is None or identity != expected:
            raise ContractError("foreign/substituted artifact must be left untouched: " + path)
        if identity["type"] == "directory":
            if self.backend.inventory(path):
                raise ContractError("foreign/nonempty directory must be left untouched")
        elif path in self.plan.files:
            self._verify_file(path, self.plan.files[path])
        self.backend.unlink(path, expected)
        return True

    def _revoke_policy(self):
        self._effect("policy_unlink:" + self.plan.policy_path,
            lambda: self._exact_remove(self.plan.policy_path), remove=(self.plan.policy_path,))
        # fsync parent even when publication never happened.
        self._effect("policy_parent_fsync:" + self.plan.policy_path,
            lambda: self.backend.fsync(self.plan.policy_path, parent_only=True)
                if self.backend.exists("etc/sudoers.d") is not None else None)
        self._effect("policy_absence_validate:" + self.plan.policy_path,
            lambda: self.backend.validate_revocation(self.plan.policy_path))

    def _remove_grant(self, path):
        identity = self.backend.exists(path)
        if identity is not None and path not in self.journal.value["objects"]:
            # A later live grant is admissible for removal only with a separate
            # externally pinned identity supplied by the remove capability.
            expected = getattr(self.backend, "live_grant_identities", {}).get(path)
            expected_digest = getattr(self.backend, "live_grant_digests", {}).get(path)
            if expected is None or identity != expected or expected_digest is None:
                raise ContractError("unjournaled live grant needs exact separate revoke authority")
            if hashlib.sha256(self.backend.read(path)).hexdigest() != expected_digest:
                raise ContractError("live grant digest substitution")
            self.backend.unlink(path, expected)
            return
        self._exact_remove(path)

    def _fence(self):
        if not self.backend.lock(LIVE_LOCK):
            raise ContractError("active execution fenced; policy revoked, byte removal incomplete")

    def _remove_tree(self, root, member_label, directory_label):
        expected_paths = sorted((p for p in self.plan.files if p.startswith(root + "/")),
                                key=lambda p: (-p.count("/"), p))
        if self.backend.exists(root) is not None:
            actual = set(self.backend.inventory(root))
            allowed = {p for p in self.journal.value["objects"] if p.startswith(root + "/")}
            if actual != allowed:
                raise ContractError("unexpected tree residue refuses removal")
        for path in expected_paths:
            self._effect(member_label + ":" + path, lambda p=path: self._exact_remove(p), remove=(path,))
        dirs = {root}
        if root == self.plan.snapshot:
            dirs.update(root + "/" + m["path"] for m in self.plan.manifest["members"] if m["type"] == "directory")
        for path in expected_paths:
            dirs.update(p for p in _parents(path) if p.startswith(root + "/"))
        for path in sorted(dirs, key=lambda p: (-p.count("/"), p)):
            self._effect(directory_label + ":" + path, lambda p=path: self._exact_remove(p), remove=(path,))

    def _remove_private(self):
        paths = {self.plan.staged_path(m) for m in self.plan.grant["members"]}
        paths.update(self.plan.stage + "/" + n for n in ("authority", "trust", "policy"))
        paths.update(self.plan.stage + "/snapshot/" + item["name"] for item in self.plan.grant["snapshot_envelope"])
        if self.backend.exists(self.plan.stage) is not None:
            actual = set(self.backend.inventory(self.plan.stage))
            allowed = {p for p in self.journal.value["objects"] if p.startswith(self.plan.stage + "/")}
            if actual != allowed:
                raise ContractError("unjournaled private residue refuses cleanup")
        for path in sorted(paths):
            self._effect("private_member_unlink:" + path, lambda p=path: self._exact_remove(p), remove=(path,))
        dirs = {self.plan.stage}
        dirs.update(self.plan.stage + "/snapshot/" + m["path"] for m in self.plan.manifest["members"] if m["type"] == "directory")
        for path in paths:
            dirs.update(p for p in _parents(path) if p.startswith(self.plan.stage + "/"))
        for path in sorted(dirs, key=lambda p: (-p.count("/"), p)):
            self._effect("private_directory_remove:" + path, lambda p=path: self._exact_remove(p), remove=(path,))

    def zero_residue(self):
        for path in (self.plan.policy_path, self.plan.live_grant_path, self.plan.live_pin_path, self.plan.live_effects_path,
                     self.plan.authority_path, self.plan.trust_path, self.plan.snapshot, self.plan.broker, self.plan.stage, JOURNAL_STAGE, LIVE_LOCK):
            if self.backend.exists(path) is not None:
                raise ContractError("removable residue remains: " + path)
        for directory in (NAMESPACE + "/authorities", NAMESPACE + "/bootstrap-trust", NAMESPACE + "/grants", INSTALL_DIRECTORY):
            if self.backend.exists(directory) is not None:
                allowed = {JOURNAL_PATH, INSTALL_LOCK, JOURNAL_STAGE} if directory == INSTALL_DIRECTORY else set()
                if not set(self.backend.inventory(directory)).issubset(allowed):
                    raise ContractError("foreign namespace residue remains")
        # Permanent attempts/evidence are intentionally not traversed, altered,
        # trusted as installation inputs, or counted as removable residue.

    def remove(self):
        if self.backend.capability.operation != "revoke-remove":
            raise ContractError("separate revoke-remove authority required")
        # First load the authenticated journal. Broader object revalidation is
        # deliberately after policy revocation: unrelated payload substitution
        # must never prevent revoking an otherwise exact privilege entry.
        bootstrap = set(_parents(JOURNAL_PATH))
        for path in self.plan.directories:
            if path in bootstrap:
                self._ensure_directory(path)
        self._lock_install()
        self.journal.load()
        suffix = None if self.journal.value is None else self.journal.value["phase"]
        lock_gone = self.backend.exists(LIVE_LOCK) is None
        if suffix in REMOVE_PHASES and REMOVE_PHASES.index("live_grant_removing") <= REMOVE_PHASES.index(suffix) <= REMOVE_PHASES.index("private_stages_removing"):
            pending = self.journal.value["pending_effect"]
            completing_last_delete = pending is not None and pending["effect"] == "execution_lock_remove:"+LIVE_LOCK and lock_gone
            already_deleted = "execution_lock_remove:"+LIVE_LOCK in self.journal.value["completed_effects"] and lock_gone
            if not (completing_last_delete or already_deleted):
                self._fence()
        if self.journal.value is not None:
            self._resolve_pending()
            pending = self.journal.value["pending_effect"]
            if pending is not None:
                # Explicitly abandon an unapplied/nonmutating install intent;
                # cancellation is distinct from an invented effect completion.
                self.journal.commit(self.journal.value["phase"], phase_boundary=False,
                    cancelled_effect=pending["effect"], boundary="cancel_intent_journal:"+pending["effect"])
        if self.journal.value is None:
            self.journal.commit("install_prepared")
            # No unpublished arbitrary payload is adopted into the journal.
        if self.journal.value["phase"] == "removed":
            self.zero_residue()
            return self.journal.value
        if self.journal.value["phase"] in INSTALL_PHASES:
            self._phase("remove_prepared")
        start = REMOVE_PHASES.index(self.journal.value["phase"])
        if start >= REMOVE_PHASES.index("live_grant_removing") and start <= REMOVE_PHASES.index("private_stages_removing") and self.backend.exists(LIVE_LOCK) is not None:
            # Kernel/advisory locks are never recovered from a journal boolean.
            # Every resumed destructive suffix reacquires the live fence.
            self._fence()
        for phase in REMOVE_PHASES[start:]:
            self._phase(phase)
            if phase == "policy_revoking":
                self._revoke_policy()
            elif phase == "live_grant_removing":
                for path in (self.plan.live_grant_path, self.plan.live_pin_path, self.plan.live_effects_path):
                    self._effect("live_grant_unlink:" + path, lambda p=path: self._remove_grant(p), remove=(path,))
                self._effect("execution_fence:" + LIVE_LOCK, self._fence, add=(LIVE_LOCK,))
                self.journal.revalidate_objects()
            elif phase == "authority_removing":
                path = self.plan.authority_path
                self._effect("authority_unlink:" + path, lambda: self._exact_remove(path), remove=(path,))
                path = self.plan.trust_path
                self._effect("authority_unlink:" + path, lambda: self._exact_remove(path), remove=(path,))
            elif phase == "snapshot_removing":
                self._remove_tree(self.plan.snapshot, "snapshot_member_unlink", "snapshot_directory_remove")
            elif phase == "broker_removing":
                self._remove_tree(self.plan.broker, "broker_member_unlink", "broker_directory_remove")
            elif phase == "private_stages_removing":
                self._remove_private()
                def remove_lock():
                    result = self._exact_remove(LIVE_LOCK)
                    fd = self.backend._locks.pop(LIVE_LOCK, None)
                    if fd is not None:
                        import os
                        self.backend.syscalls.unlock(fd)
                        os.close(fd)
                    return result
                self._effect("execution_lock_remove:" + LIVE_LOCK, remove_lock, remove=(LIVE_LOCK,))
                self._effect("zero_residue_validate:" + NAMESPACE, self.zero_residue)
        return self.journal.value
