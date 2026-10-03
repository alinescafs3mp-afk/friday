import copy
import hashlib
import unittest
from support import load_source, observe, control_semantics
c = load_source("canonical")


def declare_control_contract():
    required = ["canonical:" + key for key in ("authenticated_api", "mandatory_pin_null", "mandatory_pin_boolean", "mandatory_pin_float", "mandatory_pin_empty")]
    keys = ("positive", "duplicate", "nested_duplicate", "unknown", "missing", "noncanonical", "no_lf", "trailing", "nan", "infinity",
            "negative_infinity", "nonascii", "surrogate", "integer_form", "float_form", "wrong_root", "utf8_bom", "two_lfs", "external_digest", "schema",
            "limit_bytes", "limit_depth", "limit_count", "explicit_large_capacity", "hard_byte_ceiling", "hard_item_ceiling")
    required += ["canonical:" + key for key in keys]
    required += ["canonical:digest_" + key for key in ("short", "uppercase", "nonhex", "type")]
    required += ["canonical:integer_" + key for key in ("boolean", "float", "negative", "overflow")]
    required += ["canonical:path_" + key for key in ("empty", "absolute", "dot", "parent", "empty_component", "nul", "backslash", "nonascii", "component_length", "path_length", "depth", "newline")]
    required += ["canonical:" + key for key in ("effect_bill_version2_structural", "effect_bill_wrong_version", "effect_bill_boolean_version", "effect_bill_wrong_expected_version_type", "effect_bill_wrong_expected_identity_type", "absolute_option_boolean_type")]
    semantics = control_semantics(__name__, "CanonicalTests", [
        ("test_mandatory_authenticated_api", "PASS", ["canonical:authenticated_api"]),
        ("test_mandatory_authenticated_api", "REFUSED", ["canonical:mandatory_pin_" + key for key in ("null", "boolean", "float", "empty")]),
        ("test_positive_and_external_digest", "PASS", ["canonical:positive"]),
        ("test_json_negatives", "REFUSED", ["canonical:" + key for key in (
            "duplicate", "nested_duplicate", "unknown", "missing", "noncanonical", "no_lf", "trailing", "nan", "infinity",
            "negative_infinity", "nonascii", "surrogate", "integer_form", "float_form", "wrong_root", "utf8_bom", "two_lfs", "external_digest", "schema")]),
        ("test_bounds", "REFUSED", ["canonical:limit_" + key for key in ("bytes", "depth", "count")]),
        ("test_explicit_large_document_capacity", "PASS", ["canonical:explicit_large_capacity"]),
        ("test_explicit_large_document_capacity", "REFUSED", ["canonical:hard_byte_ceiling", "canonical:hard_item_ceiling"]),
        ("test_digest_integer_path_negatives", "REFUSED", ["canonical:digest_" + key for key in ("short", "uppercase", "nonhex", "type")]
            + ["canonical:integer_" + key for key in ("boolean", "float", "negative", "overflow")]
            + ["canonical:path_" + key for key in ("empty", "absolute", "dot", "parent", "empty_component", "nul", "backslash", "nonascii", "component_length", "path_length", "depth", "newline")]),
        ("test_exact_api_option_and_effect_bill_types", "PASS", ["canonical:effect_bill_version2_structural"]),
        ("test_exact_api_option_and_effect_bill_types", "REFUSED", ["canonical:" + key for key in ("effect_bill_wrong_version", "effect_bill_boolean_version", "effect_bill_wrong_expected_version_type", "effect_bill_wrong_expected_identity_type", "absolute_option_boolean_type")]),
    ])
    return {"matrices": {}, "required_observations": sorted(set(required)),
            "observation_semantics": semantics, "matrix_owners": {}}


class CanonicalTests(unittest.TestCase):
    def test_exact_api_option_and_effect_bill_types(self):
        value = dict(schema="friday.effect-bill.v1", bill_id="fixture-bill", version=2, scope=["fixture"],
                     allowed_effects=["fixture-effect"], forbidden_effects=["production-effect"], required_authority="fixture", implies=[])
        raw, pin = c.canonical_bytes(value), c.digest(value)
        self.assertEqual(c.parse_effect_bill(raw, pin, "fixture-bill", expected_version=2), value)
        observe("canonical", "effect_bill_version2_structural", "PASS", semantic_authority=False)
        for key, arguments in (("wrong_version", {"expected_version": 1}),
                               ("wrong_expected_version_type", {"expected_version": True}),
                               ("wrong_expected_identity_type", {"expected_bill_id": None, "expected_version": 2})):
            passed = dict(expected_bill_id="fixture-bill", **{k: v for k, v in arguments.items() if k != "expected_bill_id"})
            if "expected_bill_id" in arguments:
                passed["expected_bill_id"] = arguments["expected_bill_id"]
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                c.parse_effect_bill(raw, pin, **passed)
            observe("canonical", "effect_bill_" + key, "REFUSED")
        changed = dict(value, version=True)
        with self.assertRaises(c.ContractError):
            c.parse_effect_bill(c.canonical_bytes(changed), c.digest(changed), "fixture-bill")
        observe("canonical", "effect_bill_boolean_version", "REFUSED")
        with self.assertRaises(c.ContractError):
            c.validate_path("x", absolute=0)
        observe("canonical", "absolute_option_boolean_type", "REFUSED")

    def test_mandatory_authenticated_api(self):
        raw = c.canonical_bytes({"schema": "test.v1", "a": 1})
        self.assertEqual(c.parse_authenticated_object(raw, ("schema", "a"), expected_sha256=hashlib.sha256(raw).hexdigest(), schema="test.v1")["a"], 1)
        observe("canonical", "authenticated_api", "PASS")
        for key, value in (("null", None), ("boolean", True), ("float", 0.0), ("empty", "")):
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                c.parse_authenticated_object(raw, ("schema", "a"), expected_sha256=value)
            observe("canonical", "mandatory_pin_" + key, "REFUSED")

    def test_positive_and_external_digest(self):
        value = {"schema": "test.v1", "a": [0, True, None, "snow: \u2603"]}
        raw = c.canonical_bytes(value)
        self.assertTrue(raw.isascii())
        self.assertEqual(c.parse_canonical_object(raw, value, schema="test.v1", expected_sha256=hashlib.sha256(raw).hexdigest()), value)
        self.assertEqual(c.digest(value), hashlib.sha256(raw).hexdigest())
        observe("canonical", "positive", "PASS")

    def test_json_negatives(self):
        cases = {
            "duplicate": b'{"a":1,"a":1}\n', "nested_duplicate": b'{"a":{"b":1,"b":2}}\n',
            "unknown": b'{"a":1,"extra":2}\n', "missing": b'{}\n',
            "noncanonical": b'{ "a":1}\n', "no_lf": b'{"a":1}', "trailing": b'{"a":1}\n{}',
            "nan": b'{"a":NaN}\n', "infinity": b'{"a":Infinity}\n', "negative_infinity": b'{"a":-Infinity}\n',
            "nonascii": '{"a":"\u2603"}\n'.encode(), "surrogate": b'{"a":"\\ud800"}\n',
            "integer_form": b'{"a":01}\n', "float_form": b'{"a":1.00}\n', "wrong_root": b'[1]\n',
            "utf8_bom": b'\xef\xbb\xbf{"a":1}\n', "two_lfs": b'{"a":1}\n\n',
        }
        for key, raw in cases.items():
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                c.parse_canonical_object(raw, ("a",))
            observe("canonical", key, "REFUSED")
        with self.assertRaises(c.ContractError):
            c.parse_canonical_object(b'{"a":1}\n', ("a",), expected_sha256="0" * 64)
        observe("canonical", "external_digest", "REFUSED")
        with self.assertRaises(c.ContractError):
            c.parse_canonical_object(b'{"a":1}\n', ("a",), schema="test.v1")
        observe("canonical", "schema", "REFUSED")

    def test_bounds(self):
        cases = (("bytes", b'{"a":1}\n', {"max_bytes": 2}),
                 ("depth", b'{"a":[[[0]]]}\n', {"max_depth": 2}),
                 ("count", b'{"a":[0,1,2,3]}\n', {"max_items": 3}))
        for key, raw, limits in cases:
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                c.parse_canonical_object(raw, ("a",), **limits)
            observe("canonical", "limit_" + key, "REFUSED")

    def test_explicit_large_document_capacity(self):
        value = {"a": "x" * (c.MAX_DOCUMENT_BYTES + 1)}
        raw = c.canonical_bytes(value)
        with self.assertRaises(c.ContractError):
            c.parse_canonical_object(raw, ("a",))
        self.assertEqual(c.parse_canonical_object(raw, ("a",), max_bytes=64 << 20, max_items=10_000_000), value)
        sequence = {"a": list(range(50001))}
        raw = c.canonical_bytes(sequence)
        with self.assertRaises(c.ContractError):
            c.parse_canonical_object(raw, ("a",))
        self.assertEqual(c.parse_canonical_object(raw, ("a",), max_bytes=64 << 20, max_items=10_000_000), sequence)
        observe("canonical", "explicit_large_capacity", "PASS")
        for key, limit in (("hard_byte_ceiling", {"max_bytes": (64 << 20) + 1}), ("hard_item_ceiling", {"max_items": 10_000_001})):
            with self.subTest(key=key), self.assertRaises(c.ContractError):
                c.parse_canonical_object(b'{"a":1}\n', ("a",), **limit)
            observe("canonical", key, "REFUSED")

    def test_digest_integer_path_negatives(self):
        for key, value in (("short", "0" * 63), ("uppercase", "A" * 64), ("nonhex", "g" * 64), ("type", 2)):
            with self.subTest(digest=key), self.assertRaises(c.ContractError):
                c.validate_digest(value)
            observe("canonical", "digest_" + key, "REFUSED")
        for key, value in (("boolean", True), ("float", 1.0), ("negative", -1), ("overflow", 2**63)):
            with self.subTest(integer=key), self.assertRaises(c.ContractError):
                c.validate_integer(value)
            observe("canonical", "integer_" + key, "REFUSED")
        paths = {"empty": "", "absolute": "/x", "dot": "a/./b", "parent": "a/../b", "empty_component": "a//b",
                 "nul": "a\x00b", "backslash": "a\\b", "nonascii": "a/\u2603", "component_length": "x" * 256,
                 "path_length": "x/" * 2049, "depth": "/".join("x" for _ in range(33)), "newline": "a\nb"}
        for key, value in paths.items():
            with self.subTest(path=key), self.assertRaises(c.ContractError):
                c.validate_path(value)
            observe("canonical", "path_" + key, "REFUSED")
        self.assertEqual(c.validate_path("/usr/libexec/friday", absolute=True), "/usr/libexec/friday")
        self.assertEqual(c.validate_integer(3, maximum=3), 3)
