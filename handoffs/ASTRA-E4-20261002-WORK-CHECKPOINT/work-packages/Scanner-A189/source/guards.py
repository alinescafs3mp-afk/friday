"""Lexical member-path guards. Symlink targets are not followed."""


def normalize_member_path(name, max_name_bytes, max_depth):
    if not isinstance(name, str) or name == "":
        return None, "empty_member_name"
    if "\x00" in name:
        return None, "nul_member_name"
    raw_bytes = name.encode("utf-8")
    if len(raw_bytes) > max_name_bytes:
        return None, "resource_ceiling_exceeded"
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        return None, "absolute_member_path"
    parts = []
    for part in text.split("/"):
        if part == "" or part == ".":
            continue
        if part == "..":
            return None, "path_escape"
        parts.append(part)
    if not parts:
        if text and all(part in ("", ".") for part in text.split("/")):
            return ".", None
        return None, "empty_member_name"
    if len(parts) > max_depth:
        return None, "resource_depth_exceeded"
    return "/".join(parts), None


def casefold_collision(paths):
    folded = {}
    for path in paths:
        key = path.casefold()
        prior = folded.get(key)
        if prior is None:
            folded[key] = path
        elif prior != path:
            return path
    return None


def duplicate_path(paths):
    seen = set()
    for path in paths:
        if path in seen:
            return path
        seen.add(path)
    return None


def symlink_escapes(member_path, target):
    if not isinstance(target, str) or target == "" or "\x00" in target:
        return True
    target_text = target.replace("\\", "/")
    if target_text.startswith("/") or (len(target_text) >= 2 and target_text[1] == ":"):
        return True
    base = member_path.split("/")[:-1]
    for part in target_text.split("/"):
        if part == "" or part == ".":
            continue
        if part == "..":
            if not base:
                return True
            base.pop()
            continue
        base.append(part)
    return False
