"""Narrow privileged APT broker for Friday's host capability plane.

The package deliberately has no generic command primitive.  Its public surface
is a versioned authenticated Unix-socket protocol whose only mutating operation
executes a previously planned, human-approved APT transaction.
"""

from .approval import PackageApprovalProof, PackageApprovalSigner, PackageApprovalVerifier
from .apt_backend import AptBackend, AptExecutionResult, AptReconciliationResult, PythonAptBackend
from .authentication import BrokerAuthenticator, ReplayLedger
from .client import (
    PackageBrokerClient,
    PackageBrokerClientError,
    PackageBrokerRejected,
    PackageBrokerUnavailable,
    PackageBrokerUnknownOutcome,
    load_pinned_public_key,
)
from .contracts import (
    AptInstallPlan,
    AptTransaction,
    BrokerContractError,
    BrokerWireResponse,
    InstalledPackage,
    PackageChange,
    PackageEvidenceReference,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    PackageRef,
    PackageTransactionReceipt,
    RepositoryOrigin,
    ServiceUnitChange,
    ServiceUnitObservation,
    ServiceUnitState,
)
from .daemon import PackageBrokerDaemon
from .policy import BrokerPolicy, load_broker_policy
from .store import BrokerStore

__all__ = [
    "AptBackend",
    "AptExecutionResult",
    "AptInstallPlan",
    "AptReconciliationResult",
    "AptTransaction",
    "BrokerAuthenticator",
    "BrokerContractError",
    "BrokerPolicy",
    "BrokerStore",
    "BrokerWireResponse",
    "InstalledPackage",
    "PackageBrokerDaemon",
    "PackageBrokerClient",
    "PackageBrokerClientError",
    "PackageBrokerRejected",
    "PackageBrokerUnavailable",
    "PackageBrokerUnknownOutcome",
    "PackageApprovalProof",
    "PackageApprovalSigner",
    "PackageApprovalVerifier",
    "PackageChange",
    "PackageEvidenceReference",
    "PackagePostconditionState",
    "PackageRef",
    "PackageReconciliationReceipt",
    "PackageTransactionReceipt",
    "PythonAptBackend",
    "ReplayLedger",
    "RepositoryOrigin",
    "ServiceUnitChange",
    "ServiceUnitObservation",
    "ServiceUnitState",
    "load_broker_policy",
    "load_pinned_public_key",
]
