"""Pinned executable bounded JSON-Schema subset; strict Root role contracts.

No external validator/package install, arbitrary schema evaluation or issuer
labels confer authority. Existing Root verifies these Source bytes/schema pins
before loading; custody and signatures remain independently checked in code.
"""
import json
import os
import re
from common import Refused,INPUT_MAX,exact,json_preflight
from custody import Held


def validate(value,node,defs,depth=0):
    if depth>24:raise Refused("role_schema_depth")
    if "$ref" in node:
        name=node["$ref"]
        if not name.startswith("#/$defs/") or name[8:] not in defs:raise Refused("role_schema_ref")
        return validate(value,defs[name[8:]],defs,depth+1)
    if "oneOf" in node:
        matches=0
        for child in node["oneOf"]:
            try:validate(value,child,defs,depth+1)
            except Refused:continue
            matches+=1
        if matches!=1:raise Refused("role_schema_oneof")
        return
    kind=node.get("type")
    actual=("null" if value is None else "boolean" if type(value) is bool else
        "integer" if type(value) is int else "string" if type(value) is str else
        "array" if type(value) is list else "object" if type(value) is dict else "invalid")
    if kind is not None and actual not in (kind if type(kind) is list else [kind]):raise Refused("role_schema_type")
    if "const" in node and (type(value) is not type(node["const"]) or value!=node["const"]):raise Refused("role_schema_const")
    if "enum" in node and not any(type(value) is type(v) and value==v for v in node["enum"]):raise Refused("role_schema_enum")
    if actual=="integer":
        if not node.get("minimum",-10**21)<=value<=node.get("maximum",10**21):raise Refused("role_schema_integer")
    elif actual=="string":
        if "\0" in value or not node.get("minLength",0)<=len(value)<=node.get("maxLength",INPUT_MAX):raise Refused("role_schema_string")
        if "pattern" in node and re.fullmatch(node["pattern"],value) is None:raise Refused("role_schema_pattern")
    elif actual=="array":
        if not node.get("minItems",0)<=len(value)<=min(512,node.get("maxItems",512)):raise Refused("role_schema_array")
        if node.get("uniqueItems"):
            seen=set()
            for row in value:
                marker=json.dumps(row,sort_keys=True,ensure_ascii=True,separators=(",",":"))
                if marker in seen:raise Refused("role_schema_duplicate")
                seen.add(marker)
        if "prefixItems" in node:
            if len(value)!=len(node["prefixItems"]):raise Refused("role_schema_tuple")
            for row,child in zip(value,node["prefixItems"]):validate(row,child,defs,depth+1)
        elif "items" in node:
            for row in value:validate(row,node["items"],defs,depth+1)
    elif actual=="object":
        if not node.get("minProperties",0)<=len(value)<=min(512,node.get("maxProperties",512)):raise Refused("role_schema_object")
        fields=node.get("properties",{});required=node.get("required",[])
        if not set(required).issubset(value):raise Refused("role_schema_required")
        for key,row in value.items():
            if type(key) is not str or len(key)>240:raise Refused("role_schema_key")
            child=fields.get(key)
            if child is None:
                extra=node.get("additionalProperties",False)
                if extra is False:raise Refused("role_schema_extra")
                if type(extra) is dict:validate(row,extra,defs,depth+1)
            else:validate(row,child,defs,depth+1)


def load_role_schema(enrollment,meter):
    pins=[r for r in enrollment["source_files"] if r["relative_path"]=="schemas/friday.a138.roles.v1.json"]
    if len(pins)!=1:raise Refused("pinned_role_schema_required")
    key=(pins[0]["path"],pins[0]["sha256"],tuple(pins[0]["identity9_decimal_strings"]))
    with Held(pins[0]["path"],pins[0],meter,INPUT_MAX,True) as held:
        cached=getattr(meter,"role_schema_cache",None)
        if cached is not None and cached[0]==key:return cached[1]
        raw=held.read(INPUT_MAX)
    allocation=json_preflight(raw if raw.endswith(b"\n") else raw+b"\n",INPUT_MAX,24)
    hold=meter.reserve("strict-role-schema-allocation",reads=len(raw),allocation=allocation)
    try:
        schema=json.loads(raw)
        if schema.get("$id")!="friday.a138.roles.v1":raise Refused("role_schema_id")
        hold.commit(reads=len(raw));meter.own_result(schema,hold);hold=None
        meter.role_schema_cache=(key,schema)
        return schema
    finally:
        if hold is not None:hold.release()


def validate_role(value,role,enrollment,meter):
    schema=load_role_schema(enrollment,meter)
    if role not in schema["$defs"]:raise Refused("role_schema_role")
    validate(value,schema["$defs"][role],schema["$defs"])


def validate_admission(value,enrollment,meter):
    validate_role(value,"admission",enrollment,meter)


def validate_ordinary(value,enrollment,meter):
    validate_role(value,"ordinary",enrollment,meter)
