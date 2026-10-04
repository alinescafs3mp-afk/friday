"""Wheel filename tags. Tag expansion is correspondence data, not compatibility approval."""

import re


def pep503_name(name):
    if not isinstance(name, str) or name == "":
        return None
    return re.sub(r"[-_.]+", "-", name).lower()


def expand_compressed_tag(python_tag, abi_tag, platform_tag):
    pythons = python_tag.split(".")
    abis = abi_tag.split(".")
    platforms = platform_tag.split(".")
    expanded = []
    for python in pythons:
        for abi in abis:
            for platform in platforms:
                expanded.append(f"{python}-{abi}-{platform}")
    return expanded


_BUILD_TAG = re.compile(r"^[0-9][0-9A-Za-z]*$")


def parse_wheel_filename(filename):
    """Five fields are name, version, python, abi, platform.

    A version that starts with a digit is still the version. The optional
    sixth field is the build tag and is the only component consumed as one.
    """
    if not isinstance(filename, str) or not filename.endswith(".whl"):
        return None, "wheel_filename_suffix"
    parts = filename[:-4].split("-")
    build_tag = None
    if len(parts) == 5:
        distribution, version, python_tag, abi_tag, platform_tag = parts
    elif len(parts) == 6:
        distribution, version, build_tag, python_tag, abi_tag, platform_tag = parts
        if _BUILD_TAG.fullmatch(build_tag) is None:
            return None, "wheel_build_tag"
    else:
        return None, "wheel_filename_shape"
    if distribution == "" or version == "" or python_tag == "" or abi_tag == "" or platform_tag == "":
        return None, "wheel_filename_shape"
    parsed = {
        "distribution": distribution,
        "version": version,
        "build_tag": build_tag,
        "python_tag": python_tag,
        "abi_tag": abi_tag,
        "platform_tag": platform_tag,
        "expanded_tags": expand_compressed_tag(python_tag, abi_tag, platform_tag),
    }
    return parsed, None
