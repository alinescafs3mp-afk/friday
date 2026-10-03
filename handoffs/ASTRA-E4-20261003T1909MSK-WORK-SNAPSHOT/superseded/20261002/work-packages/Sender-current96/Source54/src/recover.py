"""Exact-request recovery. Removal is irreversible; consumed runtime only fails."""
from canonical import ContractError
from install import Installer, InstallJournal, REMOVE_PHASES


def recover_install(plan, backend, *, action="resume", fault=None):
    if action not in ("resume", "remove"):
        raise ContractError("recovery operation is fixed resume or remove")
    # The selected owner acquires the exact exclusive lock before any load can
    # promote a staged journal. No read-then-mutate recovery shortcut exists.
    if action == "remove" or backend.capability.operation == "revoke-remove":
        from uninstall import Remover
        return Remover(plan, backend, fault).remove()
    return Installer(plan, backend, fault).install()


def recover_consumed_runtime(journal, backend=None):
    from broker_runtime import recover_runtime
    return recover_runtime(journal, backend=backend)
