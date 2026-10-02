"""Structural fixtures. They are not current publisher, root, or runtime authority."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()
from pathlib import Path

from canonical import canonical_bytes, domain_digest
from resource_meter import bounded_file
from contract import ContractError
from pins import (
    EXPECTED_FIXTURE_SHA256,
    NODE_FILENAME,
    NODE_STATED_FINGERPRINT_UNVERIFIED,
    UBUNTU_ARGV,
    UBUNTU_EXECUTABLE,
    UBUNTU_FINGERPRINT,
    UBUNTU_ISSUER_ID,
    UBUNTU_KEYRING_SHA256,
)

_ARMOR = b"iQECBAEBCAAGBQAAAA=="
_CHECKSUM = b"=abcd"


def clearsign(body_lines, algorithm=b"SHA256"):
    lines = [b"-----BEGIN PGP SIGNED MESSAGE-----", b"Hash: " + algorithm, b""]
    lines.extend(body_lines)
    lines.extend([
        b"-----BEGIN PGP SIGNATURE-----",
        b"",
        _ARMOR,
        _CHECKSUM,
        b"-----END PGP SIGNATURE-----",
    ])
    return b"\n".join(lines) + b"\n"


def release_bytes(suite=b"resolute", size=b"1", digest=None):
    digest = (b"ab" * 32) if digest is None else digest
    lines = [
        b"Origin: Ubuntu",
        b"Suite: " + suite,
        b"Components: main",
        b"Architectures: amd64",
        b"SHA256:",
        b" " + digest + b" " + size + b" main/binary-amd64/Packages",
    ]
    return b"\n".join(lines) + b"\n"


def package_bytes():
    lines = [
        b"Package: demo",
        b"Version: 1",
        b"Architecture: amd64",
        b"Filename: pool/demo.deb",
        b"Size: 4",
        b"SHA256: " + (b"ab" * 32),
        b"Extra: kept",
    ]
    return b"\n".join(lines) + b"\n"


def capability(kind="ubuntu-inrelease"):
    ubuntu = kind == "ubuntu-inrelease"
    entries = []
    return {
        "schema": "friday.lab815.verifier-capability.v1",
        "document_kind": kind,
        "algorithm_class": "openpgp-sha512" if ubuntu else "openpgp-sha256",
        "argv": list(UBUNTU_ARGV) + ["inrelease" if ubuntu else "shasums256"],
        "environment_entries": entries,
        "environment_digest": domain_digest("friday.lab815.environment.v1", entries),
        "dependencies": [],
        "dependency_closure_sha256": None,
        "dependency_status": "NOT_PROVEN",
        "executable": UBUNTU_EXECUTABLE,
        "executable_sha256": None,
        "key_fingerprint": UBUNTU_FINGERPRINT if ubuntu else NODE_STATED_FINGERPRINT_UNVERIFIED,
        "keyring_sha256": UBUNTU_KEYRING_SHA256 if ubuntu else None,
        "produced_by_this_package": False,
        "signer_supplied_by_candidate": False,
        "verified_by_tool": False,
        "issuer_attestation": {
            "issuer_id": UBUNTU_ISSUER_ID if ubuntu else "nodejs.org",
            "produced_by_this_package": False,
            "attestation_sha256": None,
            "custody_sha256": None,
            "signature_sha256": None,
        },
    }


def load_bytes(name, expected_sha):
    path = Path(__file__).resolve().parent.parent / "fixtures" / name
    try:
        raw = bounded_file(path, 2000000)
    except OSError as exc:
        raise ContractError("fixture_pin") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ContractError("fixture_pin")
    return raw


def expected_fixture_bytes():
    return load_bytes("expected-bill.json", EXPECTED_FIXTURE_SHA256)


def canonical_document(document):
    return canonical_bytes(document)
