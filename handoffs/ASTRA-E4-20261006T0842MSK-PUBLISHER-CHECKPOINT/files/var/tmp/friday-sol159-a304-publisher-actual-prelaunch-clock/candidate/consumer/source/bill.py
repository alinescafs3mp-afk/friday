"""Recompute bill projections from rows. Pins live in pins.py, not beside the rows."""

from whole_join import exact_path_name
from canonical import domain_digest
from contract import ADDITIONAL_MAX, ContractError, is_digest
from pins import (
    HISTORICAL_UBUNTU_IDENTITY_SHA256,
    HISTORICAL_WHEEL_IDENTITY_SHA256,
    INDEX_COUNT,
    INDEX_PROJECTION_SHA256,
    UBUNTU_ISSUER_ID,
    UBUNTU_MINIMUM_COUNT,
    UBUNTU_PROJECTION_SHA256,
    WHEEL_COUNT,
    WHEEL_ISSUER_ID,
    ORIGINAL_ARTIFACT_MAX,
    WHEEL_PROJECTION_SHA256,
)

UBUNTU_FIELDS = (
    "architecture",
    "component",
    "filename",
    "inrelease_sha256",
    "name",
    "packages_sha256",
    "relative_path",
    "sha256",
    "size",
    "suite",
    "version",
)
WHEEL_FIELDS = (
    "abi_tag",
    "filename",
    "metadata_raw_sha256",
    "name",
    "platform_tag",
    "python_tag",
    "relative_path",
    "requires_python",
    "sha256",
    "size",
    "version",
)
INDEX_FIELDS = (
    "architecture",
    "component",
    "id",
    "inrelease_sha256",
    "max_bytes",
    "member_name",
    "observed_member_in_matched_inrelease",
    "packages_sha256",
    "size",
    "suite",
)


def _project(items, fields):
    projected = []
    for item in items:
        if type(item) is not dict:
            raise ContractError("row")
        projected.append({key: item[key] for key in fields})
    return projected


def _split_minimum(items, count, cause, extra_cause):
    if type(items) is not list or len(items) < count or len(items) > count + ADDITIONAL_MAX:
        raise ContractError(cause)
    return items[:count], items[count:]


def recompute_ubuntu_projection(packages):
    historical, extra = _split_minimum(packages, UBUNTU_MINIMUM_COUNT, "ubuntu_minimum_count", "additional_ubuntu")
    rows = _project(historical, UBUNTU_FIELDS)
    names = [row["name"] for row in rows]
    if len(set(names)) != UBUNTU_MINIMUM_COUNT:
        raise ContractError("duplicate_package_name")
    digest = domain_digest("friday.lab815.ubuntu-bill.v1", rows)
    if digest != UBUNTU_PROJECTION_SHA256:
        raise ContractError("ubuntu_projection")
    for item in extra:
        _project([item], UBUNTU_FIELDS)
        if item["name"] in names:
            raise ContractError("duplicate_package_name")
    return digest


def recompute_wheel_projection(items):
    historical, extra = _split_minimum(items, WHEEL_COUNT, "wheel_count", "additional_wheel")
    rows = _project(historical, WHEEL_FIELDS)
    digest = domain_digest("friday.lab815.wheel-bill.v1", rows)
    if digest != WHEEL_PROJECTION_SHA256:
        raise ContractError("wheel_projection")
    for item in extra:
        wheel_filename_tag(item)
    return digest


def recompute_index_projection(indexes):
    historical, extra = _split_minimum(indexes, INDEX_COUNT, "index_count", "additional_index")
    rows = _project(historical, INDEX_FIELDS)
    identities = [(row["suite"], row["component"]) for row in rows]
    if len(set(identities)) != INDEX_COUNT:
        raise ContractError("duplicate_index")
    digest = domain_digest("friday.lab815.index-bill.v1", rows)
    if digest != INDEX_PROJECTION_SHA256:
        raise ContractError("index_projection")
    for item in extra:
        _project([item], INDEX_FIELDS)
        if (item["suite"], item["component"]) in identities:
            raise ContractError("duplicate_index")
    return digest


def link_indexes(packages, indexes):
    recompute_index_projection(indexes)
    by_key = {}
    for index in indexes:
        key = (index["suite"], index["component"])
        if key in by_key:
            raise ContractError("duplicate_index")
        by_key[key] = index
    linked = 0
    for package in packages:
        key = (package["suite"], package["component"])
        index = by_key.get(key)
        if index is None:
            raise ContractError("index_link")
        if package["inrelease_sha256"] != index["inrelease_sha256"]:
            raise ContractError("index_link")
        if package["packages_sha256"] != index["packages_sha256"]:
            raise ContractError("index_link")
        if package["architecture"] not in ("amd64", "all"):
            raise ContractError("architecture")
        member = f"{package['component']}/binary-amd64/Packages"
        if index["member_name"] != member:
            raise ContractError("index_link")
        linked += 1
    if linked < UBUNTU_MINIMUM_COUNT:
        raise ContractError("index_link")
    return linked


def retain_historical_labels(recipe):
    ubuntu = recipe["ubuntu_minimum"]
    wheels = recipe["wheels"]
    if ubuntu["historical_identity_sha256"] != HISTORICAL_UBUNTU_IDENTITY_SHA256:
        raise ContractError("historical_ubuntu_identity")
    if wheels["historical_identity_sha256"] != HISTORICAL_WHEEL_IDENTITY_SHA256:
        raise ContractError("historical_wheel_identity")
    if ubuntu["bill_sha256"] != recipe["historical_bill_sha256"]:
        raise ContractError("historical_bill")
    return True


def wheel_filename_tag(item):
    filename = item["filename"]
    if (
        type(filename) is not str
        or not filename.endswith(".whl")
        or filename.count("-") < 4
        or filename.startswith("not-acquired/")
    ):
        raise ContractError("wheel_filename")
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        raise ContractError("wheel_tag")
    python_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]
    if python_tag != item["python_tag"] or abi_tag != item["abi_tag"] or platform_tag != item["platform_tag"]:
        raise ContractError("wheel_tag")
    if not is_digest(item["sha256"]) or type(item["size"]) is not int or isinstance(item["size"], bool):
        raise ContractError("wheel_bytes")
    if item["publisher_proof"] is not False:
        raise ContractError("wheel_credit")
    return {
        "filename": filename,
        "python_tag": python_tag,
        "abi_tag": abi_tag,
        "platform_tag": platform_tag,
        "publisher_proof": False,
    }


ARTIFACT_EXTRA = 22
DATA_ROLES = ("certificates", "fonts", "locales", "timezones", "nss", "glib", "gpu")


def normalize_requires_python(raw):
    if raw is None:
        return None
    if type(raw) is not str:
        raise ContractError("requires_python")
    if raw == "":
        return None
    return raw


def wheel_requires_python_view(item):
    if "requires_python_raw" in item:
        raw = item["requires_python_raw"]
        normalized = normalize_requires_python(raw)
        if item["requires_python"] != normalized:
            raise ContractError("requires_python_normalization")
        normalization = "empty_string_to_null" if raw == "" else "identity"
    else:
        raw = None
        normalized = normalize_requires_python(item["requires_python"])
        if item["requires_python"] != normalized:
            raise ContractError("requires_python_normalization")
        normalization = "historical_normalized_pin"
    tagged = wheel_filename_tag(item)
    return {
        "name": item["name"],
        "version": item["version"],
        "filename": tagged["filename"],
        "python_tag": tagged["python_tag"],
        "abi_tag": tagged["abi_tag"],
        "platform_tag": tagged["platform_tag"],
        "requires_python": normalized,
        "requires_python_raw": raw,
        "requires_python_effective": normalized,
        "normalization": normalization,
        "metadata_raw_sha256": item["metadata_raw_sha256"],
        "package_info_selector_sha256": None,
        "selected_artifact_selector_sha256": None,
        "literal_selector_status": "NOT_PROVEN",
        "sha256": item["sha256"],
        "size": item["size"],
        "publisher_proof": False,
    }


def _observation_map(observations):
    found = {}
    for item in observations or []:
        if type(item) is not dict or type(item.get("filename")) is not str:
            raise ContractError("observation")
        if item["filename"] in found:
            raise ContractError("duplicate_observation")
        found[item["filename"]] = item
    return found


def _observed_receipt(package, observations):
    item = observations.get(package["filename"])
    if item is None:
        return None, False, "NOT_PROVEN"
    status = item.get("status")
    digest = item.get("signed_result_sha256")
    held = item.get("held_bytes")
    if status != "ADMITTED" or not is_digest(digest) or held is not True:
        return None, False, "NOT_PROVEN"
    if item.get("sha256") != package["sha256"] or item.get("size") != package["size"]:
        return None, False, "NOT_PROVEN"
    return digest, held, "ADMITTED"


def _receipt_vector(receipt_index):
    if type(receipt_index) is not dict:
        return []
    rows = []
    for key in sorted(receipt_index, key=lambda item: (str(item[0]), str(item[1]))):
        item = receipt_index[key]
        if type(item) is not dict:
            raise ContractError("receipt_membership")
        rows.append({
            "body_held": item.get("body_held"),
            "custody_sha256": item.get("custody_sha256"),
            "decision": item.get("decision"),
            "diagnostic_authority": item.get("diagnostic_authority"),
            "document_kind": item.get("document_kind"),
            "armor_sha256": item.get("armor_sha256"),
            "capability_sha256": item.get("capability_sha256"),
            "expected_capability_sha256": item.get("expected_capability_sha256"),
            "input_sha256": item.get("input_sha256"),
            "key_fingerprint": item.get("key_fingerprint"),
            "normalized_body_sha256": item.get("normalized_body_sha256"),
            "path": item.get("path"),
            "raw_sha256": item.get("raw_sha256"),
            "result_sha256": item.get("result_sha256"),
            "selected_sha256": item.get("selected_sha256"),
            "sha256": item.get("sha256"),
            "size": item.get("size"),
        })
    return rows


def compose_signed_materials(recipe, observations=None, wheel_observations=None, receipt_index=None):
    packages_in = recipe["ubuntu_minimum"]["packages"]
    wheels_in = recipe["wheels"]["items"]
    indexes = recipe["ubuntu_minimum"]["indexes"]
    ubuntu_digest = recompute_ubuntu_projection(packages_in)
    wheel_digest = recompute_wheel_projection(wheels_in)
    index_digest = recompute_index_projection(indexes)
    linked = link_indexes(packages_in, indexes)
    observed = _observation_map(observations)
    packages = []
    for package in packages_in:
        signed, held, status = _observed_receipt(package, observed)
        packages.append({
            "name": package["name"],
            "version": package["version"],
            "architecture": package["architecture"],
            "filename": package["filename"],
            "sha256": package["sha256"],
            "size": package["size"],
            "member_name": f"{package['component']}/binary-amd64/Packages",
            "packages_sha256": package["packages_sha256"],
            "inrelease_sha256": package["inrelease_sha256"],
            "signed_result_sha256": signed,
            "held_bytes": held,
            "observation_status": status,
            "publisher_proof": False,
        })
        if type(receipt_index) is dict:
            receipt = receipt_index.get(("ubuntu-archive", package["filename"]))
            if type(receipt) is dict:
                packages[-1]["receipt_decision"] = receipt.get("decision")
                packages[-1]["selected_sha256"] = receipt.get("selected_sha256", receipt.get("sha256"))
                packages[-1]["custody_sha256"] = receipt.get("custody_sha256")
                packages[-1]["key_fingerprint"] = receipt.get("key_fingerprint")
                packages[-1]["capability_sha256"] = receipt.get("capability_sha256")
                packages[-1]["expected_capability_sha256"] = receipt.get("expected_capability_sha256")
                packages[-1]["result_sha256"] = receipt.get("result_sha256")
                packages[-1]["normalized_body_sha256"] = receipt.get("normalized_body_sha256")
                packages[-1]["armor_sha256"] = receipt.get("armor_sha256")
                packages[-1]["raw_sha256"] = receipt.get("raw_sha256")
                packages[-1]["input_sha256"] = receipt.get("input_sha256")
    observed_wheels = {}
    for item in wheel_observations or []:
        if type(item) is not dict or item.get("filename") in observed_wheels:
            raise ContractError("duplicate_observation")
        observed_wheels[item["filename"]] = item
    wheels = []
    for item in wheels_in:
        merged = dict(item)
        observed = observed_wheels.get(item["filename"])
        if observed is not None:
            for key in (
                "abi_tag",
                "metadata_raw_sha256",
                "name",
                "packagetype",
                "platform_tag",
                "python_tag",
                "sha256",
                "size",
                "url",
                "version",
                "yanked",
            ):
                if key in observed and key in item and observed[key] != item[key]:
                    if key in ("metadata_raw_sha256", "sha256"):
                        raise ContractError("requires_python_normalization")
                    if key == "size":
                        raise ContractError("wheel_observation")
                    raise ContractError("wheel_observation_set")
            merged["requires_python_raw"] = observed["requires_python_raw"]
            merged["requires_python"] = observed["requires_python"]
        view = wheel_requires_python_view(merged)
        if observed is not None:
            for extra in ("abi_sha256", "body_sha256", "custody_sha256", "installed_inventory_sha256", "member_sha256", "resource_sha256", "selected_record_sha256"):
                if extra in observed:
                    view[extra] = observed[extra]
        wheels.append(view)
    if observed_wheels and set(observed_wheels) != {item["filename"] for item in wheels_in}:
        raise ContractError("wheel_observation_set")
    if len(packages) + len(wheels) + 1 > ORIGINAL_ARTIFACT_MAX:
        raise ContractError("aggregate_budget")
    return {
        "ubuntu_count": len(packages),
        "wheel_count": len(wheels),
        "index_count": len(indexes),
        "linked": linked,
        "ubuntu_digest": ubuntu_digest,
        "wheel_digest": wheel_digest,
        "index_digest": index_digest,
        "issuer_ids": {"ubuntu-minimum": UBUNTU_ISSUER_ID, "wheel": WHEEL_ISSUER_ID},
        "packages": packages,
        "wheels": wheels,
        "publisher_proof": False,
        "receipt_vector": _receipt_vector(receipt_index),
    }


def _artifact(row, observed):
    row["tag"] = row.get("abi", "none")
    item = observed.get(row["filename"]) if observed else None
    if item is not None and item.get("status") == "ADMITTED":
        observed_sha = item.get("sha256")
        row_sha = row.get("sha256")
        observed_size = item.get("size")
        size_ok = type(observed_size) is int and not isinstance(observed_size, bool)
        if row_sha is None and is_digest(observed_sha):
            row["sha256"] = observed_sha
            if size_ok:
                row["size"] = observed_size
            row["key_id"] = None
        elif is_digest(observed_sha) and observed_sha == row_sha:
            if size_ok:
                row["size"] = observed_size
            row["signature_sha256"] = item.get("signature_sha256")
            row["signer_fingerprint"] = item.get("signer_fingerprint")
            row["verifier_sha256"] = item.get("verifier_sha256")
            row["key_id"] = item.get("key_id")
            row["upstream_authority"] = item.get("upstream_authority", row.get("upstream_authority"))
        else:
            row["key_id"] = None
    else:
        row["key_id"] = None
    return row


_PIN_CLASS = {
    "ubuntu-deb": "ubuntu-archive",
    "wheel": "wheel",
    "node": "node-archive",
    "browser": "browser",
    "unrar": "unrar",
    "stdlib": "native",
    "native": "native",
    "data": "data",
    "golden": "golden",
    "candidate": "candidate",
    "custody": "custody",
}


def _overlay_pins(artifacts, pins, recipe):
    if not pins:
        return
    by_kind = {}
    for pin in pins:
        by_kind.setdefault(pin.get("document_kind"), []).append(pin)
    node = recipe["node"]
    authoritative = node.get("authoritative_sha256")
    for artifact in artifacts:
        kind = _PIN_CLASS.get(artifact["class"])
        if kind is None:
            continue
        choices = by_kind.get(kind, [])
        if artifact["class"] == "node":
            if not is_digest(authoritative):
                continue
            named = [pin for pin in choices if pin.get("sha256") == authoritative and pin.get("size") == node["size"]]
            if len(named) == 1:
                artifact["sha256"] = authoritative
                artifact["size"] = node["size"]
                artifact["upstream_authority"] = "PIN_MATCHED"
            continue
        if artifact["class"] == "unrar":
            digest = recipe["unrar"].get("publisher_digest")
            named = [pin for pin in choices if is_digest(digest) and pin.get("sha256") == digest]
            if len(named) == 1:
                artifact["sha256"] = digest
                artifact["size"] = named[0]["size"]
                artifact["upstream_authority"] = "PIN_MATCHED"
            continue
        name = artifact["filename"]
        named = [pin for pin in choices if exact_path_name(str(pin.get("relative_path") or ""), name)]
        if len(named) != 1:
            continue
        pin = named[0]
        expected_sha = artifact.get("sha256")
        if is_digest(expected_sha):
            if pin.get("sha256") == expected_sha and artifact.get("size") in (None, pin.get("size")):
                if artifact.get("size") is None:
                    artifact["size"] = pin["size"]
                if artifact.get("upstream_authority") == "NOT_PROVEN":
                    artifact["upstream_authority"] = "PIN_MATCHED"
            continue
        artifact["sha256"] = pin["sha256"]
        artifact["size"] = pin["size"]


def project_artifacts(recipe, observations=None, pins=None):
    artifacts = []
    for package in recipe["ubuntu_minimum"]["packages"]:
        artifacts.append({
            "class": "ubuntu-deb",
            "filename": package["filename"],
            "format": "deb",
            "abi": package["architecture"],
            "size": package["size"],
            "sha256": package["sha256"],
            "upstream_authority": "HISTORICAL_PIN",
            "signature_sha256": None,
            "signer_fingerprint": None,
            "verifier_sha256": None,
            "publisher_proof": False,
        })
    for item in recipe["wheels"]["items"]:
        tagged = wheel_filename_tag(item)
        artifacts.append({
            "class": "wheel",
            "filename": tagged["filename"],
            "format": "wheel",
            "abi": item["abi_tag"],
            "size": item["size"],
            "sha256": item["sha256"],
            "upstream_authority": "RETAINED_BYTE",
            "signature_sha256": None,
            "signer_fingerprint": None,
            "verifier_sha256": None,
            "publisher_proof": False,
        })
    node = recipe["node"]
    artifacts.append({
        "class": "node",
        "filename": node["filename"],
        "format": "tar.xz",
        "abi": "linux-x64",
        "size": node["size"],
        "sha256": node["authoritative_sha256"] if is_digest(node["authoritative_sha256"]) else None,
        "upstream_authority": "NOT_PROVEN",
        "signature_sha256": None,
        "signer_fingerprint": None,
        "verifier_sha256": None,
        "publisher_proof": False,
    })
    for archive in recipe["browser"]["archives"]:
        artifacts.append({
            "class": "browser",
            "filename": archive["filename"],
            "format": "zip",
            "abi": "linux-x64",
            "size": archive["size"],
            "sha256": archive["sha256"],
            "upstream_authority": "NOT_PROVEN",
            "signature_sha256": None,
            "signer_fingerprint": None,
            "verifier_sha256": None,
            "publisher_proof": False,
        })
    artifacts.append({
        "class": "unrar",
        "filename": "unrar",
        "format": "unknown",
        "abi": "linux-x64",
        "size": None,
        "sha256": None,
        "upstream_authority": "NOT_PROVEN",
        "signature_sha256": None,
        "signer_fingerprint": None,
        "verifier_sha256": None,
        "publisher_proof": False,
    })
    artifacts.append({
        "class": "stdlib",
        "filename": "python3.14-stdlib",
        "format": "unknown",
        "abi": "cp314",
        "size": None,
        "sha256": None,
        "upstream_authority": "NOT_PROVEN",
        "signature_sha256": None,
        "signer_fingerprint": None,
        "verifier_sha256": None,
        "publisher_proof": False,
    })
    artifacts.append({
        "class": "native",
        "filename": "lib-dynload",
        "format": "unknown",
        "abi": "cp314",
        "size": None,
        "sha256": None,
        "upstream_authority": "NOT_PROVEN",
        "signature_sha256": None,
        "signer_fingerprint": None,
        "verifier_sha256": None,
        "publisher_proof": False,
    })
    artifacts.append({
        "class": "native",
        "filename": "loader",
        "format": "unknown",
        "abi": "linux-x64",
        "size": None,
        "sha256": None,
        "upstream_authority": "NOT_PROVEN",
        "signature_sha256": None,
        "signer_fingerprint": None,
        "verifier_sha256": None,
        "publisher_proof": False,
    })
    for role in DATA_ROLES:
        artifacts.append({
            "class": "data",
            "filename": role,
            "format": "unknown",
            "abi": "linux-x64",
            "size": None,
            "sha256": None,
            "upstream_authority": "NOT_PROVEN",
            "signature_sha256": None,
            "signer_fingerprint": None,
            "verifier_sha256": None,
            "publisher_proof": False,
        })
    for kind, filename in (
        ("candidate", "candidate"),
        ("golden", "golden"),
        ("kernel", "kernel"),
        ("key", "archive-key"),
        ("tool", "creation-tool"),
        ("recipe", "assembly-recipe"),
        ("custody", "external-custody"),
    ):
        artifacts.append({
            "class": kind,
            "filename": filename,
            "format": "unknown",
            "abi": "none",
            "size": None,
            "sha256": None,
            "upstream_authority": "NOT_PROVEN",
            "signature_sha256": None,
            "signer_fingerprint": None,
            "verifier_sha256": None,
            "publisher_proof": False,
        })
    observed = _observation_map(observations)
    artifacts = [_artifact(row, observed) for row in artifacts]
    kernel_pins=[p for p in pins or [] if p.get('document_kind')=='kernel']
    if len(kernel_pins)==2 and len({p['relative_path'] for p in kernel_pins})==2:
        template=next(row for row in artifacts if row['class']=='kernel')
        artifacts=[row for row in artifacts if row['class']!='kernel']
        for pin in sorted(kernel_pins,key=lambda p:p['relative_path']):
            row=dict(template)
            row.update({'filename':pin['relative_path'],'sha256':pin['sha256'],'size':pin['size'],
                        'upstream_authority':'PIN_MATCHED_NOT_AUTHORITY'})
            artifacts.append(row)
    _overlay_pins(artifacts, pins, recipe)
    minimum = UBUNTU_MINIMUM_COUNT + WHEEL_COUNT + ARTIFACT_EXTRA
    for row in artifacts:
        row.setdefault("custody_sha256", None)
    if len(artifacts) < minimum or len(artifacts) > ORIGINAL_ARTIFACT_MAX:
        raise ContractError("artifact_count")
    return artifacts
