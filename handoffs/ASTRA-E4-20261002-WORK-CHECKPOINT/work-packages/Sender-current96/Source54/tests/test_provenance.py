import copy
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import unittest
import types
from unittest.mock import patch
from support import load_source, observe, control_semantics
c = load_source("canonical")
p = load_source("provenance")
_KEY = None


def declare_control_contract():
    keys = ("fixture_cannot_grant_native", "verifier_binding_absent", "verifier_binding_wrong_type", "verifier_binding_wrong_digest",
            "approving_source_substitution", "approving_callback_production", "explicit_callback_fixture", "authentic_requirements_unavailable",
            "unissued_material_capability", "fixture_copy_escalation", "closed_requirements_fixture",
            "signed_incompatible_abi", "signed_incompatible_tag", "signed_incompatible_format", "signed_incompatible_class",
            "requirements_missing_subject", "requirements_unknown_subject", "requirements_duplicate_subject", "requirements_wrong_generation_type", "correct_receipt_wrong_verifier_pin")
    required = ["provenance:" + key for key in keys]
    keys = ("rsa_sha256_detached_positive", "external_provenance_digest", "absent_artifact", "extra_artifact", "artifact_bytes", "artifact_size", "filename", "format", "class", "tag", "abi", "digest",
            "signer", "key", "index", "signature", "owner_approval", "recipe", "creation_tool", "authority", "manifest", "hash_correct_signature_invalid", "hash_correct_index_wrong_identity",
            "rsa_signature_invalid", "rsa_signature_short", "rsa_signature_over_modulus", "missing_index", "missing_signature", "extra_receipt", "unavailable_verifier", "local_cache_self_manifest",
            "duplicate_artifact", "duplicate_authority", "artifact_bomb", "unsupported_algorithm", "weak_key")
    required += ["provenance:" + key for key in keys]
    required += ["provenance:schema_" + layer + "_" + operation for layer in ("top", "authority", "public_key", "artifact", "approval") for operation in ("extra", "missing")]
    required += ["provenance:" + key for key in ("same_code_foreign_verify_globals", "same_code_foreign_authority_globals", "same_code_foreign_text_globals",
        "resolved_pow_shadow", "resolved_hashlib_substitution", "resolved_canonical_dependency", "immutable_use_after_binding", "fixed_runtime_binding_positive")]
    semantics = control_semantics(__name__, "ProvenanceTests", [
        ("test_fixed_verifier_namespace_and_immutable_use", "PASS", ["provenance:fixed_runtime_binding_positive"]),
        ("test_fixed_verifier_namespace_and_immutable_use", "REFUSED", ["provenance:same_code_foreign_" + key + "_globals" for key in ("verify", "authority", "text")]
            + ["provenance:" + key for key in ("resolved_pow_shadow", "resolved_hashlib_substitution", "resolved_canonical_dependency", "immutable_use_after_binding")]),
        ("test_correctly_signed_incompatible_closed_requirements", "PASS", ["provenance:closed_requirements_fixture"]),
        ("test_correctly_signed_incompatible_closed_requirements", "REFUSED", ["provenance:signed_incompatible_" + key for key in ("abi", "tag", "format", "class")]
            + ["provenance:requirements_" + key for key in ("missing_subject", "unknown_subject", "duplicate_subject", "wrong_generation_type")]),
        ("test_verifier_implementation_and_fixture_authority", "PASS", ["provenance:explicit_callback_fixture"]),
        ("test_verifier_implementation_and_fixture_authority", "REFUSED", ["provenance:" + key for key in (
            "fixture_cannot_grant_native", "verifier_binding_absent", "verifier_binding_wrong_type", "verifier_binding_wrong_digest",
            "approving_source_substitution", "correct_receipt_wrong_verifier_pin", "approving_callback_production",
            "authentic_requirements_unavailable", "unissued_material_capability", "fixture_copy_escalation")]),
        ("test_real_signature_positive_and_external_pins", "PASS", ["provenance:rsa_sha256_detached_positive"]),
        ("test_real_signature_positive_and_external_pins", "REFUSED", ["provenance:external_provenance_digest"]),
        ("test_artifact_and_external_authority_negatives", "REFUSED", ["provenance:" + key for key in (
            "absent_artifact", "extra_artifact", "artifact_bytes", "artifact_size", "filename", "format", "class", "tag", "abi", "digest",
            "signer", "key", "index", "signature", "owner_approval", "recipe", "creation_tool", "authority", "manifest")]),
        ("test_correct_hashes_do_not_replace_cryptographic_proof", "REFUSED", ["provenance:hash_correct_signature_invalid", "provenance:hash_correct_index_wrong_identity"]),
        ("test_signature_cryptographic_and_receipt_negatives", "REFUSED", ["provenance:" + key for key in (
            "rsa_signature_invalid", "rsa_signature_short", "rsa_signature_over_modulus", "missing_index", "missing_signature", "extra_receipt", "unavailable_verifier", "local_cache_self_manifest")]),
        ("test_full_nested_exact_schemas_and_bounds", "REFUSED", ["provenance:schema_" + layer + "_" + operation for layer in ("top", "authority", "public_key", "artifact", "approval") for operation in ("extra", "missing")]
            + ["provenance:" + key for key in ("duplicate_artifact", "duplicate_authority", "artifact_bomb", "unsupported_algorithm", "weak_key")]),
    ])
    return {"matrices": {}, "required_observations": sorted(set(required)),
            "observation_semantics": semantics, "matrix_owners": {}}


def synthetic_rsa_key():
    global _KEY
    if _KEY is not None:
        return _KEY
    def prime(seed):
        raw = b"".join(hashlib.sha256(seed + bytes([index])).digest() for index in range(4))
        number = int.from_bytes(raw, "big") | (3 << 1022) | 1
        while True:
            d, shifts = number - 1, 0
            while not d & 1:
                d //= 2
                shifts += 1
            probable = True
            for base in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41):
                x = pow(base, d, number)
                if x in (1, number - 1):
                    continue
                for _ in range(shifts - 1):
                    x = x * x % number
                    if x == number - 1:
                        break
                else:
                    probable = False
                    break
            if probable and math.gcd(65537, number - 1) == 1:
                return number
            number += 2
    first, second = prime(b"SOL017 synthetic p"), prime(b"SOL017 synthetic q")
    modulus = first * second
    _KEY = (modulus, pow(65537, -1, (first - 1) * (second - 1)))
    return _KEY


def synthetic_provenance(raw=b"synthetic artifact bytes\n", filename="artifact.tar", manifest_sha256="0" * 64, artifact_changes=None):
    modulus, private = synthetic_rsa_key()
    verifier_source = Path(p.__file__).read_bytes()
    verifier_sha256 = hashlib.sha256(verifier_source).hexdigest()
    authority = dict(authority_id="synthetic-owner", algorithm="rsa-pkcs1v15-sha256",
                     signer_fingerprint="SYNTHETIC-NONPRODUCTION-KEY", key_id="fixture-key",
                     public_key=dict(modulus_hex=format(modulus, "x"), exponent=65537), verifier_sha256=verifier_sha256)
    artifact = dict(zip(p.ARTIFACT_IDENTITY_KEYS, ("rootfs", filename, "tar", "synthetic", "x86_64", len(raw), hashlib.sha256(raw).hexdigest())))
    artifact.update(artifact_changes or {})
    index = c.canonical_bytes(dict(schema="friday.artifact-index.v1", authority_id=authority["authority_id"], artifacts=[artifact]))
    suffix = p.RSASHA256Verifier.DIGEST_INFO_PREFIX + hashlib.sha256(index).digest()
    length = (modulus.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (length - len(suffix) - 3) + b"\x00" + suffix
    signature = pow(int.from_bytes(encoded, "big"), private, modulus).to_bytes(length, "big")
    artifact.update(authority_id=authority["authority_id"], signed_index_sha256=hashlib.sha256(index).hexdigest(),
                    signature_sha256=hashlib.sha256(signature).hexdigest(), signer_fingerprint=authority["signer_fingerprint"],
                    key_id=authority["key_id"], verifier_sha256=authority["verifier_sha256"])
    approval = dict(owner_id="synthetic-owner", artifact_set_sha256=c.digest(p.artifact_projection([artifact])),
                    signer_set_sha256=c.digest([authority]), manifest_sha256=manifest_sha256,
                    candidate_commit="c" * 40, candidate_tree="d" * 40, attempt_generation=1,
                    rootfs_identity_sha256="c" * 64, golden_identity_sha256="d" * 64)
    value = dict(schema=p.SCHEMA, manifest_sha256=manifest_sha256, assembly_recipe_sha256="e" * 64,
                 creation_tool_sha256="f" * 64, approved_authorities=[authority], artifacts=[artifact], owner_approval=approval)
    parsed = p.parse_material_provenance(c.canonical_bytes(value), c.digest(value))
    arguments = dict(expected_manifest_sha256=manifest_sha256, approved_authorities=[copy.deepcopy(authority)],
                     owner_approval_sha256=c.digest(approval), assembly_recipe_sha256="e" * 64,
                     creation_tool_sha256="f" * 64, artifacts={filename: raw},
                     receipts={artifact["signed_index_sha256"]: index, artifact["signature_sha256"]: signature},
                     verifier_source=verifier_source, verifier_source_sha256=verifier_sha256, fixture_authority=True)
    return parsed, arguments


def reauthenticate(value):
    value = copy.deepcopy(dict(value))
    value["owner_approval"]["artifact_set_sha256"] = c.digest(p.artifact_projection(value["artifacts"]))
    value["owner_approval"]["signer_set_sha256"] = c.digest(value["approved_authorities"])
    return p.parse_material_provenance(c.canonical_bytes(value), c.digest(value))


def synthetic_requirements(value):
    return dict(schema="friday.material-requirements.v1", artifact_identities=p.artifact_projection(value["artifacts"]),
                platform=dict(machine="x86_64", abi="linux-gnu", cpython_abi="cp314", import_suffixes=[".py", ".so"], rootfs_image_sha256="5" * 64),
                subjects=[dict(role=role, path="rootfs/" + role, sha256="a" * 64, executable_class="data") for role in p.REQUIRED_SUBJECT_ROLES],
                **{key: value["owner_approval"][key] for key in ("candidate_commit", "candidate_tree", "attempt_generation", "rootfs_identity_sha256", "golden_identity_sha256")})


class ProvenanceTests(unittest.TestCase):
    def test_fixed_verifier_namespace_and_immutable_use(self):
        value, arguments = synthetic_provenance()
        raw, pin = arguments["verifier_source"], arguments["verifier_source_sha256"]
        self.assertEqual(p.bind_fixed_verifier_source(raw, pin), pin)
        authority = value["approved_authorities"][0]
        index = arguments["receipts"][value["artifacts"][0]["signed_index_sha256"]]
        signature = arguments["receipts"][value["artifacts"][0]["signature_sha256"]]
        self.assertIs(p.RSASHA256Verifier().verify(index, signature, authority), True)
        observe("provenance", "fixed_runtime_binding_positive", "PASS", binding_revalidated_at_signature_use=True)
        for label, owner, name in (("verify", p.RSASHA256Verifier, "verify"), ("authority", p, "_authority"), ("text", p, "_text")):
            original = getattr(owner, name)
            foreign = dict(original.__globals__)
            foreign["pow"] = lambda *unused: 1
            replacement = types.FunctionType(original.__code__, foreign, original.__name__, original.__defaults__, original.__closure__)
            self.assertEqual(p._code_fingerprint(replacement.__code__), p._code_fingerprint(original.__code__))
            with patch.object(owner, name, replacement), self.assertRaises(c.ContractError):
                p.bind_fixed_verifier_source(raw, pin)
            observe("provenance", "same_code_foreign_" + label + "_globals", "REFUSED", exact_original_code=True, foreign_namespace=True)
        with patch.dict(p.__dict__, {"pow": lambda *unused: 1}), self.assertRaises(c.ContractError):
            p.bind_fixed_verifier_source(raw, pin)
        observe("provenance", "resolved_pow_shadow", "REFUSED")
        with patch.object(p.hashlib, "sha256", lambda *unused: None), self.assertRaises(c.ContractError):
            p.bind_fixed_verifier_source(raw, pin)
        observe("provenance", "resolved_hashlib_substitution", "REFUSED")
        with patch.object(p, "exact_keys", lambda value, unused: value), self.assertRaises(c.ContractError):
            p.bind_fixed_verifier_source(raw, pin)
        observe("provenance", "resolved_canonical_dependency", "REFUSED")
        # The binding was accepted before mutation; the actual signature method
        # must enforce that immutable-use binding again, independently of bind.
        self.assertEqual(p.bind_fixed_verifier_source(raw, pin), pin)
        with patch.dict(p.__dict__, {"pow": lambda *unused: 1}), self.assertRaises(c.ContractError):
            p.RSASHA256Verifier().verify(index, signature, authority)
        observe("provenance", "immutable_use_after_binding", "REFUSED", prior_binding_accepted=True, actual_verify_called=True)
        self.assertIs(p.RSASHA256Verifier().verify(index, signature, authority), True)

    def test_correctly_signed_incompatible_closed_requirements(self):
        value, arguments = synthetic_provenance()
        requirements = synthetic_requirements(value)
        bound = dict(arguments, requirements=c.canonical_bytes(requirements), requirements_sha256=c.digest(requirements))
        self.assertEqual(p.verify_owner_approved_materials(value, **bound).requirements_sha256, c.digest(requirements))
        observe("provenance", "closed_requirements_fixture", "PASS", production_authority=False)
        for key in ("abi", "tag", "format", "class"):
            incompatible, changed = synthetic_provenance(artifact_changes={key: "incompatible"})
            changed.update(requirements=bound["requirements"], requirements_sha256=bound["requirements_sha256"])
            with self.subTest(key=key), self.assertRaisesRegex(c.ContractError, "incompatible"):
                p.verify_owner_approved_materials(incompatible, **changed)
            observe("provenance", "signed_incompatible_" + key, "REFUSED", signed_receipts_recomputed=True, external_pins_recomputed=True)
        for key, mutate in (("missing_subject", lambda x: x["subjects"].pop()),
                            ("unknown_subject", lambda x: x["subjects"][0].update(role="foreign")),
                            ("duplicate_subject", lambda x: x["subjects"].__setitem__(1, dict(x["subjects"][0]))),
                            ("wrong_generation_type", lambda x: x.update(attempt_generation=True))):
            changed = copy.deepcopy(requirements)
            mutate(changed)
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                p.parse_material_requirements(c.canonical_bytes(changed), c.digest(changed))
            observe("provenance", "requirements_" + key, "REFUSED")

    def test_verifier_implementation_and_fixture_authority(self):
        value, arguments = synthetic_provenance()
        approved = p.verify_owner_approved_materials(value, **arguments)
        self.assertEqual(approved.authority_mode, "fixture")
        self.assertEqual(approved.verifier_implementation_sha256, arguments["verifier_source_sha256"])
        with self.assertRaisesRegex(c.ContractError, "fixture materials"):
            p.require_approved_materials(approved)
        observe("provenance", "fixture_cannot_grant_native", "REFUSED")
        for key, bad in (("absent", None), ("wrong_type", False), ("wrong_digest", "a" * 64)):
            changed = dict(arguments, verifier_source_sha256=bad)
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                p.verify_owner_approved_materials(value, **changed)
            observe("provenance", "verifier_binding_" + key, "REFUSED")
        changed_source = arguments["verifier_source"].replace(b"result = hmac.compare_digest(decoded, expected)", b"result = True")
        with self.assertRaisesRegex(c.ContractError, "loaded verifier implementation"):
            p.bind_fixed_verifier_source(changed_source, hashlib.sha256(changed_source).hexdigest())
        observe("provenance", "approving_source_substitution", "REFUSED")
        changed = copy.deepcopy(dict(value))
        changed["approved_authorities"][0]["verifier_sha256"] = "a" * 64
        changed["artifacts"][0]["verifier_sha256"] = "a" * 64
        changed = reauthenticate(changed)
        external = dict(arguments, approved_authorities=copy.deepcopy(changed["approved_authorities"]), owner_approval_sha256=c.digest(changed["owner_approval"]))
        with self.assertRaisesRegex(c.ContractError, "approved verifier implementation"):
            p.verify_owner_approved_materials(changed, **external)
        observe("provenance", "correct_receipt_wrong_verifier_pin", "REFUSED", external_pins_recomputed=True)
        class ApprovingCallback:
            calls = 0
            def verify(self, *unused):
                self.calls += 1
                return True
        callback = ApprovingCallback()
        changed = dict(arguments, verifier=callback, fixture_authority=False)
        with self.assertRaisesRegex(c.ContractError, "callback"):
            p.verify_owner_approved_materials(value, **changed)
        self.assertEqual(callback.calls, 0)
        observe("provenance", "approving_callback_production", "REFUSED", callback_calls=0)
        changed["fixture_authority"] = True
        modeled = p.verify_owner_approved_materials(value, **changed)
        self.assertEqual(modeled.authority_mode, "fixture")
        with self.assertRaises(c.ContractError):
            p.require_approved_materials(modeled)
        observe("provenance", "explicit_callback_fixture", "PASS", production_authority=False)
        with self.assertRaisesRegex(c.ContractError, "FORMAT_NOT_COVERED"):
            p.verify_owner_approved_materials(value, **dict(arguments, fixture_authority=False))
        observe("provenance", "authentic_requirements_unavailable", "REFUSED")
        forged = p.ApprovedMaterials(approved.provenance_sha256, approved.manifest_sha256, approved.materials_sha256,
                                    approved.assembly_recipe_sha256, approved.creation_tool_sha256, approved.artifacts,
                                    "production", approved.verifier_implementation_sha256, "a" * 64)
        with self.assertRaises(c.ContractError):
            p.require_approved_materials(forged)
        observe("provenance", "unissued_material_capability", "REFUSED")
        forged = replace(approved, authority_mode="production", requirements_sha256="a" * 64)
        with self.assertRaises(c.ContractError):
            p.require_approved_materials(forged)
        observe("provenance", "fixture_copy_escalation", "REFUSED")

    def test_real_signature_positive_and_external_pins(self):
        value, arguments = synthetic_provenance()
        self.assertEqual(c.digest(value), value.expected_sha256)
        approved = p.verify_owner_approved_materials(value, **arguments)
        self.assertEqual(approved.artifacts["artifact.tar"], b"synthetic artifact bytes\n")
        self.assertEqual(approved.manifest_sha256, "0" * 64)
        self.assertEqual(approved.materials_sha256, c.digest(p.material_identity_projection(value)))
        with self.assertRaises(TypeError):
            approved.artifacts["artifact.tar"] = b"different"
        observe("provenance", "rsa_sha256_detached_positive", "PASS")
        with self.assertRaises(c.ContractError):
            p.verify_owner_approved_materials(dict(value), **arguments)
        observe("provenance", "external_provenance_digest", "REFUSED")

    def test_artifact_and_external_authority_negatives(self):
        for key in ("absent_artifact", "extra_artifact", "artifact_bytes", "artifact_size", "filename", "format", "class", "tag", "abi", "digest",
                    "signer", "key", "index", "signature", "owner_approval", "recipe", "creation_tool", "authority", "manifest"):
            value, arguments = synthetic_provenance()
            altered = copy.deepcopy(dict(value))
            if key == "absent_artifact":
                arguments["artifacts"] = {}
            elif key == "extra_artifact":
                arguments["artifacts"]["foreign.tar"] = b"foreign"
            elif key == "artifact_bytes":
                arguments["artifacts"]["artifact.tar"] = b"x" * len(arguments["artifacts"]["artifact.tar"])
            elif key == "artifact_size":
                arguments["artifacts"]["artifact.tar"] += b"x"
            elif key in ("filename", "format", "class", "tag", "abi", "digest"):
                field = "sha256" if key == "digest" else key
                altered["artifacts"][0][field] = "a" * 64 if field == "sha256" else "foreign.tar" if field == "filename" else "foreign"
                value = reauthenticate(altered)
            elif key in ("signer", "key"):
                field = "signer_fingerprint" if key == "signer" else "key_id"
                altered["approved_authorities"][0][field] = "foreign"
                altered["artifacts"][0][field] = "foreign"
                value = reauthenticate(altered)
            elif key in ("index", "signature"):
                field = "signed_index_sha256" if key == "index" else "signature_sha256"
                receipt_digest = value["artifacts"][0][field]
                arguments["receipts"][receipt_digest] = b"tampered independent receipt"
            elif key == "owner_approval":
                arguments["owner_approval_sha256"] = "a" * 64
            elif key in ("recipe", "creation_tool", "manifest"):
                field = {"recipe": "assembly_recipe_sha256", "creation_tool": "creation_tool_sha256", "manifest": "expected_manifest_sha256"}[key]
                arguments[field] = "a" * 64
            elif key == "authority":
                arguments["approved_authorities"] = []
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                p.verify_owner_approved_materials(value, **arguments)
            observe("provenance", key, "REFUSED")

    def test_correct_hashes_do_not_replace_cryptographic_proof(self):
        value, arguments = synthetic_provenance()
        altered = copy.deepcopy(dict(value))
        original = altered["artifacts"][0]["signature_sha256"]
        invalid = b"\x00" * len(arguments["receipts"][original])
        del arguments["receipts"][original]
        replacement = hashlib.sha256(invalid).hexdigest()
        altered["artifacts"][0]["signature_sha256"] = replacement
        arguments["receipts"][replacement] = invalid
        forged = reauthenticate(altered)
        with self.assertRaisesRegex(c.ContractError, "signature verification"):
            p.verify_owner_approved_materials(forged, **arguments)
        observe("provenance", "hash_correct_signature_invalid", "REFUSED")
        value, arguments = synthetic_provenance()
        altered = copy.deepcopy(dict(value))
        original = altered["artifacts"][0]["signed_index_sha256"]
        index = c.parse_canonical_object(arguments["receipts"][original], p.INDEX_KEYS)
        index["artifacts"][0]["abi"] = "foreign-abi"
        bad_index = c.canonical_bytes(index)
        del arguments["receipts"][original]
        replacement = hashlib.sha256(bad_index).hexdigest()
        altered["artifacts"][0]["signed_index_sha256"] = replacement
        arguments["receipts"][replacement] = bad_index
        forged = reauthenticate(altered)
        with self.assertRaisesRegex(c.ContractError, "signed index artifact identity"):
            p.verify_owner_approved_materials(forged, **arguments)
        observe("provenance", "hash_correct_index_wrong_identity", "REFUSED")

    def test_signature_cryptographic_and_receipt_negatives(self):
        value, arguments = synthetic_provenance()
        signature_digest = value["artifacts"][0]["signature_sha256"]
        signature = arguments["receipts"][signature_digest]
        authority = value["approved_authorities"][0]
        index = arguments["receipts"][value["artifacts"][0]["signed_index_sha256"]]
        verifier = p.RSASHA256Verifier()
        for key, raw in (("rsa_signature_invalid", bytes(len(signature))), ("rsa_signature_short", signature[:-1]),
                         ("rsa_signature_over_modulus", bytes.fromhex(authority["public_key"]["modulus_hex"]))):
            with self.subTest(key=key):
                self.assertIs(verifier.verify(index, raw, authority), False)
            observe("provenance", key, "REFUSED")
        for key in ("missing_index", "missing_signature", "extra_receipt", "unavailable_verifier", "local_cache_self_manifest"):
            value, arguments = synthetic_provenance()
            if key == "missing_index":
                del arguments["receipts"][value["artifacts"][0]["signed_index_sha256"]]
            elif key == "missing_signature":
                del arguments["receipts"][value["artifacts"][0]["signature_sha256"]]
            elif key == "extra_receipt":
                arguments["receipts"]["a" * 64] = b"extra"
            elif key == "unavailable_verifier":
                arguments["verifier"] = object()
            else:
                arguments["receipts"] = {}
                arguments["approved_authorities"] = []
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                p.verify_owner_approved_materials(value, **arguments)
            observe("provenance", key, "REFUSED")

    def test_full_nested_exact_schemas_and_bounds(self):
        value, _ = synthetic_provenance()
        for key, path in (("top", ()), ("authority", ("approved_authorities", 0)), ("public_key", ("approved_authorities", 0, "public_key")),
                          ("artifact", ("artifacts", 0)), ("approval", ("owner_approval",))):
            for operation in ("extra", "missing"):
                altered = copy.deepcopy(dict(value))
                node = altered
                for piece in path:
                    node = node[piece]
                if operation == "extra":
                    node["foreign"] = 1
                else:
                    del node[next(iter(node))]
                with self.subTest(key=key, operation=operation), self.assertRaises(c.ContractError):
                    p.parse_material_provenance(c.canonical_bytes(altered), c.digest(altered))
                observe("provenance", "schema_" + key + "_" + operation, "REFUSED")
        for key, mutate in (("duplicate_artifact", lambda x: x["artifacts"].append(copy.deepcopy(x["artifacts"][0]))),
                            ("duplicate_authority", lambda x: x["approved_authorities"].append(copy.deepcopy(x["approved_authorities"][0]))),
                            ("artifact_bomb", lambda x: x["artifacts"][0].update(size=p.MAX_ARTIFACT_BYTES + 1)),
                            ("unsupported_algorithm", lambda x: x["approved_authorities"][0].update(algorithm="unknown")),
                            ("weak_key", lambda x: x["approved_authorities"][0]["public_key"].update(modulus_hex="f" * 256))):
            altered = copy.deepcopy(dict(value))
            mutate(altered)
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                reauthenticate(altered)
            observe("provenance", key, "REFUSED")
