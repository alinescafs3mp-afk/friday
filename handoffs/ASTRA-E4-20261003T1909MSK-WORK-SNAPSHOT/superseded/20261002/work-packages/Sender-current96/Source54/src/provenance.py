"""Offline external authority validation; diagnostic bytes never confer trust."""
from dataclasses import dataclass
import hashlib
import hmac
import marshal
import re
import sys
import types
import weakref
from types import MappingProxyType
from canonical import (ContractError, canonical_bytes, digest, exact_keys,
                       parse_canonical_object, validate_digest, validate_integer, validate_path)

SCHEMA = "friday.material-provenance.v1"
PROVENANCE_KEYS = ("schema", "manifest_sha256", "assembly_recipe_sha256", "creation_tool_sha256",
                   "approved_authorities", "artifacts", "owner_approval")
AUTHORITY_KEYS = ("authority_id", "algorithm", "signer_fingerprint", "key_id", "public_key",
                  "verifier_sha256")
PUBLIC_KEY_KEYS = ("modulus_hex", "exponent")
ARTIFACT_KEYS = ("class", "filename", "format", "tag", "abi", "size", "sha256", "authority_id",
                 "signed_index_sha256", "signature_sha256", "signer_fingerprint", "key_id", "verifier_sha256")
ARTIFACT_IDENTITY_KEYS = ("class", "filename", "format", "tag", "abi", "size", "sha256")
APPROVAL_KEYS = ("owner_id", "artifact_set_sha256", "signer_set_sha256", "manifest_sha256",
                 "candidate_commit", "candidate_tree", "attempt_generation", "rootfs_identity_sha256",
                 "golden_identity_sha256")
INDEX_KEYS = ("schema", "authority_id", "artifacts")
MAX_ARTIFACTS = 1024
MAX_ARTIFACT_BYTES = 1 << 30
MAX_TOTAL_BYTES = 1 << 34
REQUIREMENT_KEYS = ("schema", "artifact_identities", "platform", "subjects", "candidate_commit",
                    "candidate_tree", "attempt_generation", "rootfs_identity_sha256", "golden_identity_sha256")
REQUIREMENT_PLATFORM_KEYS = ("machine", "abi", "cpython_abi", "import_suffixes", "rootfs_image_sha256")
SUBJECT_KEYS = ("role", "path", "sha256", "executable_class")
REQUIRED_SUBJECT_ROLES = ("broker_package", "candidate_controller", "cpython", "golden", "kernel_contract",
                          "loader_contract", "node", "preflight", "rootfs", "runtime_contract", "toolchain")
_MATERIAL_SEAL = object()
_ISSUED_MATERIALS = {}


class AuthenticatedProvenance(dict):
    def __init__(self, value, expected_sha256):
        super().__init__(value)
        self.expected_sha256 = expected_sha256


def _text(value, maximum=256):
    if type(value) is not str or not value or len(value) > maximum or not value.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ContractError("bounded canonical authority text")
    return value


def _authority(value):
    exact_keys(value, AUTHORITY_KEYS)
    for key in ("authority_id", "signer_fingerprint", "key_id"):
        _text(value[key])
    validate_digest(value["verifier_sha256"])
    if value["algorithm"] != "rsa-pkcs1v15-sha256":
        raise ContractError("unsupported signing algorithm")
    public = exact_keys(value["public_key"], PUBLIC_KEY_KEYS)
    modulus = public["modulus_hex"]
    if type(modulus) is not str or re.fullmatch(r"[1-9a-f][0-9a-f]{511,2047}", modulus) is None or len(modulus) % 2:
        raise ContractError("RSA modulus must be canonical 2048..8192 bits")
    number = int(modulus, 16)
    if number.bit_length() < 2048 or not number & 1:
        raise ContractError("RSA modulus strength/form")
    if public["exponent"] != 65537 or type(public["exponent"]) is not int:
        raise ContractError("fixed RSA exponent")
    return value


def artifact_projection(artifacts):
    return [{key: item[key] for key in ARTIFACT_IDENTITY_KEYS} for item in artifacts]


def material_identity_projection(value):
    return {"artifacts": value["artifacts"], "approved_authorities": value["approved_authorities"],
            "assembly_recipe_sha256": value["assembly_recipe_sha256"],
            "creation_tool_sha256": value["creation_tool_sha256"]}


def parse_material_provenance(raw, expected_sha256):
    validate_digest(expected_sha256)
    value = parse_canonical_object(raw, PROVENANCE_KEYS, schema=SCHEMA, expected_sha256=expected_sha256)
    for key in ("manifest_sha256", "assembly_recipe_sha256", "creation_tool_sha256"):
        validate_digest(value[key])
    authorities = value["approved_authorities"]
    if type(authorities) is not list or not authorities or len(authorities) > 128:
        raise ContractError("authority count")
    for authority in authorities:
        _authority(authority)
    names = [authority["authority_id"] for authority in authorities]
    if names != sorted(set(names)):
        raise ContractError("authority order/duplicate")
    authority_map = {item["authority_id"]: item for item in authorities}
    artifacts = value["artifacts"]
    if type(artifacts) is not list or not artifacts or len(artifacts) > MAX_ARTIFACTS:
        raise ContractError("artifact count")
    total = 0
    filenames = []
    for artifact in artifacts:
        exact_keys(artifact, ARTIFACT_KEYS)
        for key in ("class", "format", "tag", "abi", "authority_id", "signer_fingerprint", "key_id"):
            _text(artifact[key])
        filename = validate_path(artifact["filename"])
        if "/" in filename:
            raise ContractError("artifact canonical basename")
        filenames.append(filename)
        validate_integer(artifact["size"], minimum=1, maximum=MAX_ARTIFACT_BYTES)
        total += artifact["size"]
        for key in ("sha256", "signed_index_sha256", "signature_sha256", "verifier_sha256"):
            validate_digest(artifact[key])
        authority = authority_map.get(artifact["authority_id"])
        if authority is None or any(artifact[key] != authority[key] for key in ("signer_fingerprint", "key_id", "verifier_sha256")):
            raise ContractError("artifact signer/key/verifier mismatch")
    if total > MAX_TOTAL_BYTES or filenames != sorted(set(filenames)) or len({name.casefold() for name in filenames}) != len(filenames):
        raise ContractError("artifact inventory/order/bound")
    approval = exact_keys(value["owner_approval"], APPROVAL_KEYS)
    _text(approval["owner_id"])
    for key in ("artifact_set_sha256", "signer_set_sha256", "manifest_sha256", "rootfs_identity_sha256", "golden_identity_sha256"):
        validate_digest(approval[key])
    for key in ("candidate_commit", "candidate_tree"):
        if type(approval[key]) is not str or re.fullmatch(r"[0-9a-f]{40}", approval[key]) is None:
            raise ContractError("approval candidate identity")
    validate_integer(approval["attempt_generation"], minimum=1, maximum=2**31 - 1)
    if approval["manifest_sha256"] != value["manifest_sha256"] or approval["artifact_set_sha256"] != digest(artifact_projection(artifacts)) or approval["signer_set_sha256"] != digest(authorities):
        raise ContractError("approval binding mismatch")
    return AuthenticatedProvenance(value, expected_sha256)


class RSASHA256Verifier:
    """Strict bounded RSASSA-PKCS1-v1_5 SHA256 verification, stdlib-only.

    Approved public keys and verifier source digests are external authority input.
    No key discovery, network, subprocess, cache or signature-format guessing.
    """
    DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
    __slots__ = ()

    def verify(self, message, signature, authority):
        _check_fixed_verifier_runtime()
        _authority(authority)
        if type(message) is not bytes or len(message) > 2_097_152 or type(signature) is not bytes:
            raise ContractError("bounded signature inputs required")
        modulus = int(authority["public_key"]["modulus_hex"], 16)
        length = (modulus.bit_length() + 7) // 8
        if len(signature) != length or int.from_bytes(signature, "big") >= modulus:
            return False
        decoded = pow(int.from_bytes(signature, "big"), 65537, modulus).to_bytes(length, "big")
        suffix = self.DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
        padding = b"\xff" * (length - len(suffix) - 3)
        if len(padding) < 8:
            return False
        expected = b"\x00\x01" + padding + b"\x00" + suffix
        result = hmac.compare_digest(decoded, expected)
        _check_fixed_verifier_runtime()
        return result


def _code_fingerprint(code):
    constants = tuple(_code_fingerprint(item) if isinstance(item, types.CodeType) else item for item in code.co_consts)
    normalized = code.replace(co_filename="<authenticated-verifier>", co_consts=constants)
    return hashlib.sha256(marshal.dumps(normalized)).digest()


def bind_fixed_verifier_source(raw, expected_sha256):
    """Bind approved bytes to the actual loaded fixed verifier and dependencies.

    Compiles for code comparison only; supplied source is never executed. A matching
    declaration alone cannot authenticate a substituted loaded implementation.
    """
    _check_fixed_verifier_runtime()
    validate_digest(expected_sha256)
    if type(raw) is not bytes or not raw or len(raw) > 2_097_152 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractError("verifier source external digest mismatch")
    try:
        compiled = compile(raw, "<authenticated-verifier>", "exec", dont_inherit=True)
    except (ValueError, SyntaxError) as exc:
        raise ContractError("verifier source compilation refused") from exc
    definitions = {item.co_name: item for item in compiled.co_consts if isinstance(item, types.CodeType)}
    cls = definitions.get("RSASHA256Verifier")
    methods = {} if cls is None else {item.co_name: item for item in cls.co_consts if isinstance(item, types.CodeType)}
    for name, actual in (("_authority", _authority), ("_text", _text)):
        expected = definitions.get(name)
        if expected is None or type(actual) is not types.FunctionType or _code_fingerprint(expected) != _code_fingerprint(actual.__code__):
            raise ContractError("loaded verifier dependency implementation mismatch")
    actual = RSASHA256Verifier.__dict__.get("verify")
    if "verify" not in methods or type(actual) is not types.FunctionType or _code_fingerprint(methods["verify"]) != _code_fingerprint(actual.__code__):
        raise ContractError("loaded verifier implementation mismatch")
    if RSASHA256Verifier.DIGEST_INFO_PREFIX != bytes.fromhex("3031300d060960864801650304020105000420"):
        raise ContractError("loaded verifier algorithm constant mismatch")
    return expected_sha256


def _capture_fixed_verifier_runtime():
    """Capture the authentic loaded namespace and resolved closed dependencies.

    This is a sampled immutable-use binding under the reviewed module lifetime,
    not proof that arbitrary in-process mutation or host bootstrap is excluded.
    """
    functions = (RSASHA256Verifier.verify, _authority, _text, exact_keys, validate_digest)
    bound = []
    def names(code):
        result = set(code.co_names)
        for item in code.co_consts:
            if isinstance(item, types.CodeType):
                result.update(names(item))
        return result
    for function in functions:
        namespace = function.__globals__
        builtins = function.__builtins__
        resolved = tuple((name, namespace[name] if name in namespace else builtins[name], name in namespace)
                         for name in sorted(names(function.__code__))
                         if name in namespace or name in builtins)
        bound.append((function, function.__code__, function.__defaults__, function.__kwdefaults__, function.__closure__,
                      namespace, namespace.get("__name__"), builtins, resolved))
    return (RSASHA256Verifier, RSASHA256Verifier.DIGEST_INFO_PREFIX, tuple(bound),
            ((hashlib, "sha256", hashlib.sha256), (hmac, "compare_digest", hmac.compare_digest),
             (re, "fullmatch", re.fullmatch)),
            tuple((module.__name__, module) for module in (hashlib, hmac, re)))


def _check_fixed_verifier_runtime(binding=None):
    if binding is None:
        binding = _FIXED_VERIFIER_BINDING
    cls, prefix, functions, attributes, modules = binding
    if RSASHA256Verifier is not cls or cls.__dict__.get("verify") is not functions[0][0] or cls.DIGEST_INFO_PREFIX != prefix:
        raise ContractError("loaded verifier immutable-use binding mismatch")
    for function, code, defaults, kwdefaults, closure, namespace, name, builtins, resolved in functions:
        module = sys.modules.get(name)
        if module is None or module.__dict__ is not namespace or function.__globals__ is not namespace or function.__builtins__ is not builtins:
            raise ContractError("loaded verifier origin/global namespace mismatch")
        if function.__code__ is not code or function.__defaults__ is not defaults or function.__kwdefaults__ is not kwdefaults or function.__closure__ is not closure:
            raise ContractError("loaded verifier immutable function drift")
        for key, expected, in_globals in resolved:
            if (key in namespace) != in_globals or (namespace.get(key) if in_globals else builtins.get(key)) is not expected:
                raise ContractError("loaded verifier resolved dependency drift")
    for module, key, expected in attributes:
        if getattr(module, key, None) is not expected:
            raise ContractError("loaded verifier resolved module dependency drift")
    for name, module in modules:
        if sys.modules.get(name) is not module:
            raise ContractError("loaded verifier dependency origin drift")


_FIXED_VERIFIER_BINDING = _capture_fixed_verifier_runtime()


def parse_material_requirements(raw, expected_sha256):
    validate_digest(expected_sha256)
    value = parse_canonical_object(raw, REQUIREMENT_KEYS, schema="friday.material-requirements.v1", expected_sha256=expected_sha256)
    identities = value["artifact_identities"]
    if type(identities) is not list or not identities or len(identities) > MAX_ARTIFACTS:
        raise ContractError("closed material requirements inventory")
    for item in identities:
        exact_keys(item, ARTIFACT_IDENTITY_KEYS)
        for key in ("class", "filename", "format", "tag", "abi"):
            _text(item[key])
        validate_path(item["filename"])
        if "/" in item["filename"]:
            raise ContractError("requirement artifact basename")
        validate_integer(item["size"], minimum=1, maximum=MAX_ARTIFACT_BYTES)
        validate_digest(item["sha256"])
    names = [item["filename"] for item in identities]
    if names != sorted(set(names)):
        raise ContractError("requirement artifact order/duplicate")
    platform = exact_keys(value["platform"], REQUIREMENT_PLATFORM_KEYS)
    if platform["machine"] != "x86_64":
        raise ContractError("requirement platform")
    for key in ("abi", "cpython_abi"):
        _text(platform[key])
    suffixes = platform["import_suffixes"]
    if type(suffixes) is not list or not suffixes or len(suffixes) > 16 or any(type(item) is not str or not item.startswith(".") or "/" in item or "\\" in item or len(item) > 128 for item in suffixes) or suffixes != sorted(set(suffixes)):
        raise ContractError("requirement import suffixes")
    validate_digest(platform["rootfs_image_sha256"])
    subjects = value["subjects"]
    if type(subjects) is not list or len(subjects) != len(REQUIRED_SUBJECT_ROLES):
        raise ContractError("complete required subject set")
    for subject in subjects:
        exact_keys(subject, SUBJECT_KEYS)
        validate_path(subject["path"])
        validate_digest(subject["sha256"])
        if subject["executable_class"] not in ("data", "executable"):
            raise ContractError("subject executable class")
    if [item["role"] for item in subjects] != list(REQUIRED_SUBJECT_ROLES):
        raise ContractError("closed ordered subject roles")
    for key in ("candidate_commit", "candidate_tree"):
        if type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
            raise ContractError("requirement candidate identity")
    validate_integer(value["attempt_generation"], minimum=1, maximum=2**31 - 1)
    for key in ("rootfs_identity_sha256", "golden_identity_sha256"):
        validate_digest(value[key])
    return value


@dataclass(frozen=True)
class ApprovedMaterials:
    provenance_sha256: str
    manifest_sha256: str
    materials_sha256: str
    assembly_recipe_sha256: str
    creation_tool_sha256: str
    artifacts: object
    authority_mode: str = "unapproved"
    verifier_implementation_sha256: object = None
    requirements_sha256: object = None
    _seal: object = None


def require_approved_materials(value, *, fixture_authority=False):
    if type(fixture_authority) is not bool or type(value) is not ApprovedMaterials or value._seal is not _MATERIAL_SEAL:
        raise ContractError("issued exact material capability required")
    issued = _ISSUED_MATERIALS.get(id(value))
    if issued is None or issued[0]() is not value or issued[1] != _material_capability_projection(value):
        raise ContractError("material capability was not issued or was changed")
    if value.authority_mode != "production" and not (fixture_authority is True and value.authority_mode == "fixture"):
        raise ContractError("fixture materials cannot grant production authority")
    if value.authority_mode == "production":
        validate_digest(value.verifier_implementation_sha256)
        validate_digest(value.requirements_sha256)
    return value


def _material_capability_projection(value):
    return (value.provenance_sha256, value.manifest_sha256, value.materials_sha256,
            value.assembly_recipe_sha256, value.creation_tool_sha256, value.authority_mode,
            value.verifier_implementation_sha256, value.requirements_sha256,
            tuple(sorted((name, hashlib.sha256(raw).hexdigest()) for name, raw in value.artifacts.items())))


def verify_owner_approved_materials(provenance, *, expected_manifest_sha256,
                                     approved_authorities, owner_approval_sha256,
                                     assembly_recipe_sha256, creation_tool_sha256,
                                     artifacts, receipts=None, verifier=None,
                                     expected_provenance_sha256=None, fixture_authority=False,
                                     verifier_source=None, verifier_source_sha256=None,
                                     requirements=None, requirements_sha256=None):
    if type(fixture_authority) is not bool:
        raise ContractError("exact fixture authority boolean required")
    external = expected_provenance_sha256
    if external is None and isinstance(provenance, AuthenticatedProvenance):
        external = provenance.expected_sha256
    if external is None:
        raise ContractError("external provenance digest required")
    value = parse_material_provenance(canonical_bytes(dict(provenance)), external)
    for expected in (expected_manifest_sha256, owner_approval_sha256, assembly_recipe_sha256, creation_tool_sha256):
        validate_digest(expected)
    if value["manifest_sha256"] != expected_manifest_sha256 or value["assembly_recipe_sha256"] != assembly_recipe_sha256 or value["creation_tool_sha256"] != creation_tool_sha256:
        raise ContractError("external manifest/recipe/tool mismatch")
    if type(approved_authorities) is not list or value["approved_authorities"] != approved_authorities:
        raise ContractError("external approved authority set mismatch")
    if digest(value["owner_approval"]) != owner_approval_sha256:
        raise ContractError("external owner approval mismatch")
    if type(artifacts) is not dict or set(artifacts) != {item["filename"] for item in value["artifacts"]}:
        raise ContractError("absent/extra externally approved artifacts")
    if type(receipts) is not dict:
        raise ContractError("independent detached receipts required")
    verifier = RSASHA256Verifier() if verifier is None else verifier
    if not callable(getattr(verifier, "verify", None)):
        raise ContractError("signature verification capability unavailable")
    implementation = None
    if type(verifier) is RSASHA256Verifier:
        implementation = bind_fixed_verifier_source(verifier_source, verifier_source_sha256)
        if any(authority["verifier_sha256"] != implementation for authority in value["approved_authorities"]):
            raise ContractError("approved verifier implementation digest mismatch")
    elif fixture_authority is not True:
        raise ContractError("approving callback cannot grant production authority")
    requirements_value = None
    if requirements is not None or requirements_sha256 is not None:
        requirements_value = parse_material_requirements(requirements, requirements_sha256)
    authorities = {item["authority_id"]: item for item in value["approved_authorities"]}
    approved = {}
    required_receipts = set()
    for artifact in value["artifacts"]:
        raw = artifacts[artifact["filename"]]
        if type(raw) is not bytes or len(raw) != artifact["size"] or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise ContractError("artifact bytes/size/digest mismatch")
        index_digest = artifact["signed_index_sha256"]
        signature_digest = artifact["signature_sha256"]
        required_receipts |= {index_digest, signature_digest}
        index = receipts.get(index_digest)
        signature = receipts.get(signature_digest)
        if type(index) is not bytes or type(signature) is not bytes or hashlib.sha256(signature).hexdigest() != signature_digest:
            raise ContractError("detached index/signature missing or changed")
        index_object = parse_canonical_object(index, INDEX_KEYS, schema="friday.artifact-index.v1", expected_sha256=index_digest)
        expected_projection = artifact_projection([item for item in value["artifacts"] if item["authority_id"] == artifact["authority_id"]])
        if index_object["authority_id"] != artifact["authority_id"] or index_object["artifacts"] != expected_projection:
            raise ContractError("signed index artifact identity mismatch")
        if verifier.verify(index, signature, authorities[artifact["authority_id"]]) is not True:
            raise ContractError("detached signature verification failed")
        approved[artifact["filename"]] = raw
    if set(receipts) != required_receipts:
        raise ContractError("extra or unbound provenance receipts")
    if requirements_value is not None:
        if artifact_projection(value["artifacts"]) != requirements_value["artifact_identities"]:
            raise ContractError("authenticated material incompatible with required identities")
        if any(value["owner_approval"][key] != requirements_value[key] for key in ("candidate_commit", "candidate_tree", "attempt_generation", "rootfs_identity_sha256", "golden_identity_sha256")):
            raise ContractError("owner approval required subject identity mismatch")
    if fixture_authority is not True:
        # Raw publisher conversion, complete toolchain closure and authentic host
        # requirements remain unavailable. No declaration creates that proof.
        raise ContractError("FORMAT_NOT_COVERED: authentic production material requirements unavailable")
    result = ApprovedMaterials(external, expected_manifest_sha256, digest(material_identity_projection(value)),
                               assembly_recipe_sha256, creation_tool_sha256, MappingProxyType(approved),
                               "fixture", implementation, requirements_sha256, _MATERIAL_SEAL)
    key = id(result)
    _ISSUED_MATERIALS[key] = (weakref.ref(result, lambda unused, key=key: _ISSUED_MATERIALS.pop(key, None)), _material_capability_projection(result))
    return result
