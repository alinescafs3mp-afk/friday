"""Strict key checks. Schema files are loaded from the sealed schemas directory.

This is not a full JSON Schema engine. It enforces type, const, enum, required,
and additionalProperties false for the documents the public chain actually builds.
"""

import json
import os
import re

INDEX_SCHEMA = "friday.astra.e4.lab821.scan-index.v1"
HELD_SCHEMA = "friday.astra.e4.lab821.held-custody.v1"
ADMISSION_SCHEMA = "friday.astra.e4.lab821.scan-admission.v1"
RESULT_SCHEMA = "friday.astra.e4.lab821.scan-result.v1"
RESOURCE_SCHEMA = "friday.astra.e4.lab818.scan-resource.v1"
WHEEL_SCHEMA = "friday.astra.e4.lab818.wheel-metadata-observation.v1"
DEB_SCHEMA = "friday.astra.e4.lab818.deb-member-observation.v1"
MEMBER_SCHEMA = "friday.astra.e4.lab818.member-record.v1"
EXPECTATION_SCHEMA = "friday.astra.e4.lab818.archive-expectation.v1"
CUSTODY_SCHEMA = "friday.astra.e4.lab818.custody-record.v1"
RETAINED_INDEX_SCHEMA = "friday.astra.e4.lab818.retained-archive-input-index.v1"

# A schema snapshot belongs to one owned public call. Process-global caches do
# not establish the identity/lifetime of a later selected schema roster.
SCHEMA_FILES = (
    "archive-expectation.v1.json", "broker-material-provenance.v1.keys.json",
    "broker-snapshot-manifest.v1.keys.json", "custody-record.v1.json",
    "deb-member-observation.v1.json", "future-scan-manifest.v1.json",
    "held-custody.v1.json", "input-index.v1.json", "member-record.v1.json",
    "scan-admission.v1.json", "scan-index.v1.json", "scan-resource.v1.json",
    "scan-result.v1.json", "wheel-metadata-observation.v1.json",
)


def schema_directory():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "schemas")


def load_schemas(resources=None, meter=None):
    if isinstance(meter,dict) and isinstance(meter.get("schema_snapshot"),dict):
        return meter["schema_snapshot"]["documents"],None
    if not isinstance(meter,dict):
        from .bounds import new_meter
        meter = new_meter()
    directory = schema_directory()
    if not os.path.isdir(directory):
        return None, "schema_unknown"
    found = {}
    pins = {}
    identities = {}
    from .bounds import reserve
    tick = reserve(resources,meter,work_bytes=14 * 4096,live_bytes=14 * 8192)
    if tick:
        return None,tick
    from .custody import owned_metadata_directory
    directory_cause = owned_metadata_directory(directory,SCHEMA_FILES,resources,meter)
    directory_cause = directory_cause or ('held_fd_unretained' if meter.get('fd_close_uncertainties') else None)
    if directory_cause:
        return None,directory_cause
    for name in SCHEMA_FILES:
        path = os.path.join(directory, name)
        from .custody import owned_metadata_bytes
        raw,pin,cause = owned_metadata_bytes(path,1048576,resources,meter)
        cause = cause or ('held_fd_unretained' if meter.get('fd_close_uncertainties') else None)
        if cause:
            return None,cause
        from .context import _administrative
        document = _administrative(raw)
        if not isinstance(document, dict):
            return None, "schema_unknown"
        if isinstance(document.get("$id"), str):
            key = document["$id"]
        elif isinstance(document.get("envelope_schema"), str):
            key = document["envelope_schema"]
        else:
            key = document.get("schema")
        if not isinstance(key, str) or key in found:
            return None, "schema_unknown"
        found[key] = document
        pins[name] = {"sha256":pin["sha256"],"size":pin["size"]}
        identities[name] = pin["identity9"]
    if len(found) != 14:
        return None, "schema_unknown"
    meter["schema_snapshot"] = {"documents":found,"pins":pins,"identities":identities}
    return found, None


def bind_schema_roster(roster, resources, meter):
    """The embedding pins every actual schema byte; enumeration is not selection."""
    from .bounds import reserve
    if not isinstance(roster, dict) or set(roster) != set(SCHEMA_FILES):
        return "schema_unknown"
    for item in roster.values():
        if not isinstance(item, dict) or set(item) != {"sha256", "size"}:
            return "schema_unknown"
        if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0 or item["size"] > 1048576:
            return "schema_unknown"
    _schemas, cause = load_schemas(resources, meter)
    if cause:
        return cause
    snapshot = meter["schema_snapshot"]
    if snapshot["pins"] != roster:
        return "schema_unknown"
    from .custody import stat_identity9
    for name in SCHEMA_FILES:
        tick = reserve(resources,meter,work_bytes=4096)
        if tick:
            return tick
        if stat_identity9(os.stat(os.path.join(schema_directory(),name),follow_symlinks=False)) != snapshot["identities"][name]:
            return "custody_identity_changed"
    return None


def validate_role(role, document, resources, meter):
    """Strict administrative roles are definitions in the existing14 inventory."""
    found,cause = load_schemas(resources,meter)
    if cause:
        return cause
    definition = found[RESOURCE_SCHEMA].get("$defs",{}).get(role)
    if not isinstance(definition,dict):
        return "schema_unknown"
    return _value_schema(document,definition,resources,meter)


def _type_ok(value, expected):
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
    return False


def _bounds_ok(value, schema):
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        if "pattern" in schema:
            try:
                matched = re.fullmatch(schema["pattern"], value) is not None
            except re.error as raw_origin:
                from tools.native_support import retain_source_origin
                retain_source_origin(None,raw_origin)
                return False
            if not matched:
                return False
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
    return True


def _value_schema(value, schema, resources=None, meter=None):
    if isinstance(meter, dict):
        from .bounds import charge
        tick = charge(resources, meter, work_bytes=64 + (len(value) if isinstance(value, str) else 0))
        if tick:
            return tick
    if not isinstance(schema, dict):
        return "schema_unknown"
    if "$ref" in schema:
        target = schema["$ref"]
        if not isinstance(target,str):
            return "schema_unknown"
        schema_id,separator,pointer = target.partition("#")
        found,cause = load_schemas(resources,meter)
        if cause:
            return cause
        resolved = found.get(schema_id)
        if separator:
            if not pointer.startswith("/$defs/") or pointer.count("/") != 2:
                return "schema_unknown"
            resolved = resolved.get("$defs",{}).get(pointer[7:]) if isinstance(resolved,dict) else None
        if not isinstance(resolved,dict):
            return "schema_unknown"
        return _value_schema(value,resolved,resources,meter)
    for option in schema.get("allOf",[]):
        nested = _value_schema(value,option,resources,meter)
        if nested:
            return nested
    if "if" in schema:
        condition = _value_schema(value,schema["if"],resources,meter)
        if condition not in (None,"schema_rejected"):
            return condition
        branch = schema.get("then") if condition is None else schema.get("else")
        if branch is not None:
            nested = _value_schema(value,branch,resources,meter)
            if nested:
                return nested
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            nested = _value_schema(value, option, resources, meter)
            if nested not in (None, "schema_rejected", "schema_unknown"):
                return nested
            if nested is None:
                matches += 1
        if matches != 1:
            return "schema_rejected"
        return None
    if "const" in schema and value != schema["const"]:
        return "schema_rejected"
    if "enum" in schema and value not in schema["enum"]:
        return "schema_rejected"
    if "type" in schema and not _type_ok(value, schema["type"]):
        return "schema_rejected"
    if not _bounds_ok(value, schema):
        return "schema_rejected"
    if isinstance(value, dict) and (schema.get("properties") or "additionalProperties" in schema or "required" in schema):
        return _check(value, schema, resources, meter,clauses_checked=True)
    item_schema = schema.get("items")
    if isinstance(value, list) and isinstance(item_schema, dict):
        for item in value:
            nested = _value_schema(item, item_schema, resources, meter)
            if nested is not None:
                return nested
    return None


def _check(document, schema, resources=None, meter=None,clauses_checked=False):
    if not isinstance(schema, dict):
        return "schema_unknown"
    if "$ref" in schema:
        return _value_schema(document,schema,resources,meter)
    if not clauses_checked:
        for option in schema.get("allOf",[]):
            nested = _value_schema(document,option,resources,meter)
            if nested:
                return nested
    if "oneOf" in schema and "properties" not in schema:
        return _value_schema(document, schema, resources, meter)
    if schema.get("type") == "object" and not isinstance(document, dict):
        return "schema_rejected"
    if not isinstance(document, dict):
        return "schema_rejected"
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False and any(key not in properties for key in document):
        return "schema_rejected"
    for key in schema.get("required") or []:
        if key not in document:
            return "schema_rejected"
    for key, value in document.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                prop = additional
            else:
                continue
        nested = _value_schema(value, prop, resources, meter)
        if nested is not None:
            return nested
    return None


def validate_value(schema_name, document, resources=None, meter=None):
    found, cause = load_schemas(resources, meter)
    if cause is not None:
        return cause
    schema = found.get(schema_name)
    if schema is None:
        return "schema_unknown"
    if isinstance(schema, dict) and "exact_top_level_keys" in schema and "properties" not in schema:
        return "schema_unknown"
    return _check(document, schema, resources, meter)


def validate_document(document, schema_name, resources=None, meter=None):
    if not isinstance(document, dict) or not isinstance(schema_name, str):
        return "schema_rejected"
    if document.get("schema") != schema_name:
        return "schema_rejected"
    return validate_value(schema_name, document, resources, meter)
