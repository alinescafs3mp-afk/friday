"""Operator entry point for the unprivileged Friday host agent."""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import stat
import subprocess
import sys
from pathlib import Path

from friday.host_control.adapters.jq import JQ_SPEC, JqAdapter
from friday.host_control.adapters.nmap import NMAP_SPEC, NmapAdapter, probe_nmap_version
from friday.host_control.network_approval import (
    NetworkApprovalLedger,
    NetworkApprovalVerifier,
    load_network_approval_public_key,
)
from friday_package_broker.client import PackageBrokerClient, load_pinned_public_key

from .adapter_registry import AdapterRegistry
from .authentication import HMACAuthenticator, ReplayGuard
from .daemon import HostAgentDaemon
from .inventory import DpkgPackageResolver, ExecutableInventory
from .job_store import AgentJobStore
from .network_policy import load_agent_network_policy
from .process_runner import ProcessRunner, SystemdUserBackend


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="friday-host-agent")
    parser.add_argument("--socket", required=True, metavar="PRIVATE_UNIX_SOCKET")
    parser.add_argument("--key-file", required=True, metavar="PRIVATE_HMAC_KEY")
    parser.add_argument("--state-dir", required=True, metavar="PRIVATE_DIRECTORY")
    parser.add_argument("--job-root", required=True, metavar="SHARED_JOB_DIRECTORY")
    parser.add_argument("--network-policy", required=True, metavar="ROOT_OWNED_TOML")
    parser.add_argument(
        "--network-approval-public-key-file",
        required=True,
        metavar="ROOT_OWNED_ED25519_PUBLIC_KEY",
    )
    parser.add_argument("--agent-id", default="local-user-agent")
    parser.add_argument("--allowed-peer-uid", action="append", required=True, type=int)
    parser.add_argument("--max-concurrency", default=2, type=int)
    parser.add_argument("--build-id", default="development")
    parser.add_argument("--broker-socket")
    parser.add_argument("--broker-key-file")
    parser.add_argument("--broker-signing-public-key-file")
    parser.add_argument("--broker-id", default="local-package-broker")
    parser.add_argument("--check-config", action="store_true")
    return parser


def _private_user_directory(path: str | Path, *, label: str) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or "\x00" in str(selected):
        raise ValueError(f"{label} must be absolute")
    if selected.is_symlink() or selected.resolve(strict=True) != selected:
        raise ValueError(f"{label} must be canonical")
    observed = selected.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError(f"{label} has unsafe ownership or permissions")
    return selected


def _read_hmac_key(path: str | Path, *, broker_key: bool = False) -> bytes:
    selected = Path(path)
    if not selected.is_absolute() or selected.is_symlink() or selected.resolve(strict=True) != selected:
        raise ValueError("host-control key path is unsafe")
    descriptor = -1
    try:
        descriptor = os.open(
            selected,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        observed = os.fstat(descriptor)
        owner_allowed = observed.st_uid == os.geteuid() or (broker_key and observed.st_uid == 0)
        permissions_unsafe = (
            observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            if observed.st_uid == os.geteuid()
            else observed.st_mode & (stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO)
        )
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or not owner_allowed
            or permissions_unsafe
            or not 32 <= observed.st_size <= 64
        ):
            raise ValueError("host-control key metadata is unsafe")
        payload = os.read(descriptor, 65)
        after = os.fstat(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(payload) != observed.st_size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
        or b"\x00" in payload
        or b"\n" in payload
        or b"\r" in payload
    ):
        raise ValueError("host-control key material is invalid")
    return payload


def _version_probe(arguments: tuple[str, ...]):  # noqa: ANN202
    def probe(executable: str, executable_fd: int) -> str:
        argv = (executable, *arguments)
        result = subprocess.run(  # noqa: S603 - executable and arguments are code-owned
            argv,
            executable=f"/proc/self/fd/{executable_fd}",
            pass_fds=(executable_fd,),
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=3,
        )
        output = result.stdout or result.stderr
        if result.returncode != 0 or not output or len(output) > 4096:
            raise ValueError("application version probe failed")
        return output.decode("utf-8", errors="replace").splitlines()[0][:240]

    return probe


def _require_exact_peer_uid(peer_uids: list[int], *, runtime_uid: int) -> None:
    """Keep production UDS authentication bound to the agent owner's one UID."""

    if runtime_uid <= 0 or peer_uids != [runtime_uid]:
        raise ValueError("host-agent peer uid must exactly match its non-root runtime user")


def _package_client(args: argparse.Namespace) -> PackageBrokerClient | None:
    configured = (
        args.broker_socket,
        args.broker_key_file,
        args.broker_signing_public_key_file,
    )
    if not any(configured):
        return None
    if not all(configured):
        raise ValueError("package-broker arguments must be configured together")
    return PackageBrokerClient(
        socket_path=args.broker_socket,
        broker_id=args.broker_id,
        request_key=_read_hmac_key(args.broker_key_file, broker_key=True),
        pinned_public_key=load_pinned_public_key(args.broker_signing_public_key_file),
        timeout_sec=300.0,
    )


async def _run(args: argparse.Namespace) -> None:
    if os.geteuid() == 0:
        raise ValueError("friday-host-agent must run as the selected desktop user")
    if not args.agent_id or len(args.agent_id) > 64 or not args.build_id or len(args.build_id) > 120:
        raise ValueError("host-agent identity or build id is invalid")
    _require_exact_peer_uid(args.allowed_peer_uid, runtime_uid=os.geteuid())
    if not 1 <= args.max_concurrency <= 8:
        raise ValueError("host-agent concurrency is invalid")
    state_dir = _private_user_directory(args.state_dir, label="host-agent state directory")
    job_root = _private_user_directory(args.job_root, label="host-agent job directory")
    sealed_input_root = state_dir / "sealed-inputs"
    sealed_input_root.mkdir(mode=0o700, exist_ok=True)
    sealed_input_root = _private_user_directory(
        sealed_input_root,
        label="agent-private sealed input directory",
    )
    agent_key = _read_hmac_key(args.key_file)
    socket_path = Path(args.socket)
    if not socket_path.is_absolute() or "\x00" in str(socket_path):
        raise ValueError("host-agent socket path is invalid")

    adapters = (NmapAdapter(), JqAdapter())
    inventory = ExecutableInventory(
        (NMAP_SPEC, JQ_SPEC),
        package_resolver=DpkgPackageResolver(),
        version_probes={
            "data.jq": _version_probe(("--version",)),
            "network.nmap": probe_nmap_version,
        },
        allowed_owner_uids=(0,),
    )
    package_client = _package_client(args)
    network_policy_source = functools.partial(load_agent_network_policy, args.network_policy)
    # Validate ownership, schema, CIDRs and public gating before either the
    # offline preflight succeeds or the socket can be opened.
    network_policy_source()
    network_approval_verifier = NetworkApprovalVerifier(
        load_network_approval_public_key(args.network_approval_public_key_file)
    )
    boundary = SystemdUserBackend(probe_base=state_dir)
    if args.check_config:
        # Offline installation may intentionally leave the user manager stopped.
        # Validate every static launch/secret/path/package/adapter contract here; normal
        # startup still performs the live systemd-run probe before opening UDS.
        if not boundary.static_available():
            raise ValueError("trusted host execution dependencies are unavailable")
        inventory.snapshot()
        return
    if not boundary.available():
        raise ValueError("trusted systemd user execution boundary is unavailable")

    replay = ReplayGuard(state_dir / "replay.sqlite3")
    jobs = AgentJobStore(state_dir / "jobs.sqlite3")
    network_approvals = NetworkApprovalLedger(state_dir / "network-approvals.sqlite3")
    daemon = HostAgentDaemon(
        agent_id=args.agent_id,
        authenticator=HMACAuthenticator(
            agent_key,
            agent_id=args.agent_id,
        ),
        replay_guard=replay,
        inventory=inventory,
        registry=AdapterRegistry(
            adapters,
            inventory=inventory,
            network_policy=network_policy_source,
        ),
        allowed_peer_uids=frozenset(args.allowed_peer_uid),
        build_id=args.build_id,
        runner=ProcessRunner(
            boundary,
            workspace_base=job_root,
            sealed_input_base=sealed_input_root,
        ),
        job_store=jobs,
        job_root=job_root,
        max_concurrency=args.max_concurrency,
        package_client=package_client,
        network_approval_verifier=network_approval_verifier,
        network_approval_ledger=network_approvals,
    )
    try:
        await daemon.serve(socket_path)
    finally:
        network_approvals.close()
        jobs.close()
        replay.close()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        asyncio.run(_run(arguments))
    except (OSError, RuntimeError, ValueError):
        sys.stderr.write("friday-host-agent: startup rejected\n")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
