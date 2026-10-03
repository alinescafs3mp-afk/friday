"""Real canonical bill parser and exact no-wildcard policy controls."""
import unittest
from canonical import ContractError, canonical_bytes, parse_effect_bill
from support import PACKAGE, sha256, observe, control_semantics

BILLS = ("private-source-preparation", "download-only-provenance", "root-install",
         "live-one-attempt", "revoke-remove")


def declare_control_contract():
    required = sorted(["effect-bill:"+name for name in BILLS]+["policy:exact-template"])
    return {"matrices":{}, "required_observations": required,
        "observation_semantics": control_semantics(__name__, "EffectBillControls", [
            ("test_exact_independent_bills", "PASS", ["effect-bill:" + name for name in BILLS]),
            ("test_no_wildcard_fixed_policy", "PASS", ["policy:exact-template"])]),
        "matrix_owners": {}}


class EffectBillControls(unittest.TestCase):
    def test_exact_independent_bills(self):
        for name in BILLS:
            with self.subTest(bill=name):
                raw = (PACKAGE / "effects" / (name + ".v1.json")).read_bytes()
                bill = parse_effect_bill(raw, sha256(raw), name, expected_version=2 if name in ("root-install", "revoke-remove") else 1)
                self.assertEqual(bill["implies"], [])
                self.assertTrue(bill["required_authority"])
                self.assertFalse(set(bill["allowed_effects"]) & set(bill["forbidden_effects"]))
                observe("effect-bill", name, "PASS", bill_sha256=sha256(raw))

    def test_wrong_expected_digest_and_bill_do_not_authorise(self):
        for name in BILLS:
            raw = (PACKAGE / "effects" / (name + ".v1.json")).read_bytes()
            for kind, expected_digest, expected_name in (
                ("digest", "0" * 64, name), ("cross-bill", sha256(raw), "foreign")):
                with self.subTest(bill=name, rejection=kind):
                    with self.assertRaises(ContractError):
                        parse_effect_bill(raw, expected_digest, expected_name)

    def test_exact_keys_canonical_and_no_implication(self):
        import json
        raw = (PACKAGE / "effects/private-source-preparation.v1.json").read_bytes()
        original = json.loads(raw)
        changes = {"extra-key": dict(original, extra=True),
                   "implies-install": dict(original, implies=["root-install"]),
                   "version-bool": dict(original, version=True),
                   "version-other": dict(original, version=2),
                   "effect-duplicate": dict(original, allowed_effects=["x", "x"]),
                   "effect-overlap": dict(original, forbidden_effects=original["allowed_effects"]),
                   "authority-empty": dict(original, required_authority=""),
                   "schema": dict(original, schema="foreign")}
        for key, value in changes.items():
            raw = canonical_bytes(value)
            with self.subTest(rejection=key):
                with self.assertRaises(ContractError):
                    parse_effect_bill(raw, sha256(raw), "private-source-preparation")
        for key in original:
            value = dict(original)
            del value[key]
            raw = canonical_bytes(value)
            with self.subTest(missing=key):
                with self.assertRaises(ContractError):
                    parse_effect_bill(raw, sha256(raw), "private-source-preparation")

    def test_no_wildcard_fixed_policy(self):
        raw = (PACKAGE / "templates/friday-quality-gate.sudoers.in").read_bytes()
        expected = (b"#@CALLER_UID@ ALL=(root) NOPASSWD:NOSETENV: /usr/bin/python3.14 -I -B -S "
                    b"/usr/libexec/friday/quality-gate-broker-v1/@PACKAGE_SHA256@/broker_bootstrap.py run-v1\n")
        self.assertEqual(raw, expected)
        rendered = raw.replace(b"@CALLER_UID@", b"1000").replace(b"@PACKAGE_SHA256@", b"a" * 64)
        self.assertNotIn(b"*", rendered)
        self.assertNotIn(b"?", rendered)
        self.assertNotIn(b"SETENV:", rendered.replace(b"NOSETENV:", b""))
        self.assertEqual(rendered.count(b"run-v1"), 1)
        observe("policy", "exact-template", "PASS", template_sha256=sha256(raw))
