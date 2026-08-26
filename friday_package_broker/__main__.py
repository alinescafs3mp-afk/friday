"""Operator entry point for the privileged package broker."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import stat
import sys
from pathlib import Path

from .approval import PackageApprovalVerifier, load_broker_approval_public_key
from .apt_backend import PythonAptBackend
from .authentication import BrokerAuthenticator, ReplayLedger
from .contracts import BrokerContractError
from .daemon import PackageBrokerDaemon
from .evidence import PackageEvidenceStore
from .policy import load_broker_policy
from .store import BrokerStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="friday-package-broker")
    parser.add_argument("--socket", default="/run/friday-package-broker/broker.sock", metavar="ABSOLUTE_PATH")
    parser.add_argument("--policy", default="/etc/friday/package-broker.toml", metavar="ROOT_OWNED_TOML")
    parser.add_argument("--key-file", default="/etc/friday/package-broker.key", metavar="ROOT_OWNED_KEY")
    parser.add_argument(
        "--signing-key-file",
        default="/etc/friday/package-broker-signing.key",
        metavar="ROOT_ONLY_ED25519_SEED",
    )
    parser.add_argument(
        "--approval-verification-public-key-file",
        default="/etc/friday/package-backend-approval-signing.pub",
        metavar="ROOT_OWNED_ED25519_PUBLIC_KEY",
    )
    parser.add_argument("--state-dir", default="/var/lib/friday-package-broker", metavar="PRIVATE_DIRECTORY")
    parser.add_argument("--build-id", default="development", metavar="RELEASE_ID")
    parser.add_argument("--systemd-socket", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    return parser


def _read_key(path: str | Path) -> bytes:
    selected = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != 0
            or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IROTH)
            or observed.st_size > 65
        ):
            raise BrokerContractError("broker key file metadata is unsafe")
        payload = os.read(descriptor, 66)
    except OSError as exc:
        raise BrokerContractError("broker key could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if not 32 <= len(payload) <= 64 or b"\x00" in payload or b"\n" in payload or b"\r" in payload:
        raise BrokerContractError("broker key material is invalid")
    return payload


def _read_signing_key(path: str | Path) -> bytes:
    selected = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != 0
            or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or observed.st_size != 32
        ):
            raise BrokerContractError("broker signing key file metadata is unsafe")
        payload = os.read(descriptor, 33)
    except OSError as exc:
        raise BrokerContractError("broker signing key could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != 32:
        raise BrokerContractError("broker signing key material is invalid")
    return payload


def _private_state_dir(path: str | Path, *, create: bool) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or "\x00" in str(selected):
        raise BrokerContractError("broker state directory must be absolute")
    if selected.resolve(strict=False) != selected:
        raise BrokerContractError("broker state directory cannot traverse symlinks")
    if not selected.exists():
        if not create:
            parent = selected.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != 0
                or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise BrokerContractError("broker state parent directory is unsafe")
            return selected
        selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    if selected.resolve(strict=True) != selected:
        raise BrokerContractError("broker state directory cannot traverse symlinks")
    observed = selected.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise BrokerContractError("broker state directory metadata is unsafe")
    if create:
        os.chmod(selected, 0o700)
    return selected


def _validate_socket_argument(path: str | Path) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or "\x00" in str(selected):
        raise BrokerContractError("broker socket path must be absolute")
    parent = selected.parent
    if parent.resolve(strict=False) != parent:
        raise BrokerContractError("broker socket path cannot traverse symlinks")
    if parent.exists():
        observed = parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise BrokerContractError("broker socket parent directory is unsafe")
    return selected


async def _run(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise BrokerContractError("friday-package-broker requires root")
    state_dir = _private_state_dir(args.state_dir, create=not args.check_config)
    socket_path = _validate_socket_argument(args.socket)
    policy = load_broker_policy(args.policy, require_root_owner=True)
    authenticator = BrokerAuthenticator(
        _read_key(args.key_file),
        broker_id=policy.broker_id,
        signing_private_key=_read_signing_key(args.signing_key_file),
    )
    approval_verifier = PackageApprovalVerifier(
        load_broker_approval_public_key(args.approval_verification_public_key_file)
    )
    if args.check_config:
        health = PythonAptBackend().health()
        if health.manager_version in {"unavailable", "unknown"}:
            raise BrokerContractError("python-apt is unavailable")
        return
    store = BrokerStore(state_dir / "broker-state.sqlite3")
    replay = ReplayLedger(state_dir / "broker-replay.sqlite3")
    daemon = PackageBrokerDaemon(
        policy=policy,
        authenticator=authenticator,
        replay_ledger=replay,
        store=store,
        backend=PythonAptBackend(evidence_store=PackageEvidenceStore(state_dir / "evidence")),
        approval_verifier=approval_verifier,
        build_id=args.build_id,
    )
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    registered_signals: list[signal.Signals] = []
    for selected_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(selected_signal, shutdown_requested.set)
        except (NotImplementedError, RuntimeError):
            continue
        registered_signals.append(selected_signal)
    try:
        await daemon.serve(
            socket_path,
            systemd_socket=args.systemd_socket,
            shutdown_requested=shutdown_requested,
        )
    finally:
        for selected_signal in registered_signals:
            loop.remove_signal_handler(selected_signal)
        replay.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        asyncio.run(_run(arguments))
    except (BrokerContractError, OSError, RuntimeError, ValueError):
        sys.stderr.write("friday-package-broker: startup rejected\n")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
