"""Recursive exact-schema validator. Consumers compile a pinned schema before use."""

import json
import re

from contract import ContractError

PATTERN_NAMES = {
    "digest": re.compile(r"^[0-9a-f]{64}$"),
    "sha512": re.compile(r"^[0-9a-f]{128}$"),
    "fingerprint": re.compile(r"^[0-9A-F]{40}$"),
    "commit": re.compile(r"^[0-9a-f]{40}$"),
    "package_name": re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$"),
    "version": re.compile(r"^[A-Za-z0-9.+:~-]{1,128}$"),
    "filename": re.compile(r"^[A-Za-z0-9._+/~-]{1,200}$"),
    "wheel_filename": re.compile(r"^[A-Za-z0-9._+-]{1,120}\.whl$"),
    "relative_path": re.compile(r"^[A-Za-z0-9._+/~-]{1,180}$"),
    "member_name": re.compile(r"^[a-z0-9][a-z0-9.+-]*/binary-amd64/Packages$"),
    "token": re.compile(r"^[A-Za-z0-9._:/-]{1,80}$"),
    "cause": re.compile(r"^[a-z0-9_]{1,64}$"),
    "tag": re.compile(r"^[A-Za-z0-9._+-]{1,80}$"),
    "requires_python": re.compile(r"^[A-Za-z0-9._<>=!,*]{1,80}$"),
    "requires_python_raw": re.compile(r"^[A-Za-z0-9._<>=!,*]{0,80}$"),
    "field_name": re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$"),
    "abi": re.compile(r"^[A-Za-z0-9._+-]{1,40}$"),
}

SCHEMA_TYPES = {"object", "array", "string", "integer", "boolean", "const", "nullable", "enum"}
DIALECT = "friday.lab815.bounded-schema.v1"


def _schema_node(node, depth):
    if depth > 16 or type(node) is not dict:
        raise ContractError("schema_shape")
    kind = node.get("type")
    if kind not in SCHEMA_TYPES:
        raise ContractError("schema_type")
    if kind == "object":
        fields = node.get("fields")
        if node.get("exact") is not True or type(fields) is not dict or len(fields) > 64:
            raise ContractError("schema_object")
        for key, child in fields.items():
            if type(key) is not str:
                raise ContractError("schema_field")
            _schema_node(child, depth + 1)
        return
    if kind == "array":
        if type(node.get("min")) is not int or type(node.get("max")) is not int:
            raise ContractError("schema_array")
        if node["min"] < 0 or node["max"] < node["min"] or node["max"] > 512:
            raise ContractError("schema_array")
        _schema_node(node.get("items"), depth + 1)
        return
    if kind == "string":
        if type(node.get("min")) is not int or type(node.get("max")) is not int:
            raise ContractError("schema_string")
        pattern = node.get("pattern")
        if pattern is not None and pattern not in PATTERN_NAMES:
            raise ContractError("schema_pattern")
        return
    if kind == "integer":
        if type(node.get("minimum")) is not int or type(node.get("maximum")) is not int:
            raise ContractError("schema_integer")
        return
    if kind == "boolean":
        return
    if kind == "const":
        if "value" not in node:
            raise ContractError("schema_const")
        return
    if kind == "enum":
        values = node.get("values")
        if type(values) is not list or not values or len(values) > 32:
            raise ContractError("schema_enum")
        return
    _schema_node(node.get("of"), depth + 1)


def compile_schema(document):
    if type(document) is not dict:
        raise ContractError("schema_document")
    if document.get("dialect") != DIALECT:
        raise ContractError("schema_dialect")
    if type(document.get("name")) is not str or type(document.get("max_depth")) is not int:
        raise ContractError("schema_name")
    if not 1 <= document["max_depth"] <= 12:
        raise ContractError("schema_depth")
    _schema_node(document.get("root"), 1)
    return document


def validate_document(document, schema, depth=1):
    if type(schema) is not dict or schema.get("dialect") != DIALECT:
        raise ContractError("schema")
    if depth > schema["max_depth"]:
        raise ContractError("depth")
    _validate(document, schema["root"], depth, schema["max_depth"])


def validate_node(document, schema, path):
    if type(schema) is not dict or type(path) is not tuple:
        raise ContractError("schema")
    node = schema["root"]
    for part in path:
        if node.get("type") != "object" or part not in node["fields"]:
            raise ContractError("schema_path")
        node = node["fields"][part]
    _validate(document, node, 1, schema["max_depth"])


def _validate(document, node, depth, max_depth):
    if depth > max_depth:
        raise ContractError("depth")
    kind = node["type"]
    if kind == "nullable":
        if document is None:
            return
        _validate(document, node["of"], depth, max_depth)
        return
    if kind == "object":
        if type(document) is not dict:
            raise ContractError("object")
        fields = node["fields"]
        if set(document) != set(fields):
            raise ContractError("exact_keys")
        for key, child in fields.items():
            _validate(document[key], child, depth + 1, max_depth)
        return
    if kind == "array":
        if type(document) is not list:
            raise ContractError("array")
        if not node["min"] <= len(document) <= node["max"]:
            raise ContractError("count")
        seen = set()
        for item in document:
            _validate(item, node["items"], depth + 1, max_depth)
            if node.get("unique"):
                if type(item) is str:
                    marker = item
                else:
                    marker = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                if marker in seen:
                    raise ContractError("duplicate_item")
                seen.add(marker)
        return
    if kind == "string":
        if type(document) is not str or not node["min"] <= len(document) <= node["max"]:
            raise ContractError("string")
        pattern = node.get("pattern")
        if pattern is not None and PATTERN_NAMES[pattern].fullmatch(document) is None:
            raise ContractError("pattern")
        return
    if kind == "integer":
        if type(document) is not int or isinstance(document, bool):
            raise ContractError("integer")
        if not node["minimum"] <= document <= node["maximum"]:
            raise ContractError("integer_bounds")
        return
    if kind == "boolean":
        if type(document) is not bool:
            raise ContractError("boolean")
        if "const" in node and document is not node["const"]:
            raise ContractError("const")
        return
    if kind == "const":
        if document != node["value"]:
            raise ContractError("const")
        return
    if kind == "enum":
        if document not in node["values"]:
            raise ContractError("enum")
        return
    raise ContractError("schema_type")
