"""Requires-Python raw facts stay distinct from the explicit empty-string normalization.

texttable's bill value is the empty string. brotli's bill value is null.
Empty-string-to-null is a labeled normalization. It does not rewrite the raw
fact, does not repin historical bytes, and is not a compatibility pass.
An absent metadata header is null, not an invented empty string.
"""


def normalize_requires_python(raw):
    if raw is None:
        return None
    if isinstance(raw, str) and raw == "":
        return None
    if isinstance(raw, str):
        return raw
    return None


def requires_python_correspondence(bill_raw, metadata_raw):
    bill_norm = normalize_requires_python(bill_raw) if isinstance(bill_raw, str) or bill_raw is None else None
    meta_norm = normalize_requires_python(metadata_raw) if isinstance(metadata_raw, str) or metadata_raw is None else None
    empty_applied = bill_raw == "" or metadata_raw == ""
    return {
        "bill_raw": bill_raw,
        "metadata_raw": metadata_raw,
        "bill_normalized": bill_norm,
        "metadata_normalized": meta_norm,
        "raw_equal": bill_raw == metadata_raw,
        "normalized_equal": bill_norm == meta_norm,
        "empty_string_to_null_applied": empty_applied,
        "normalization_is_not_compatibility_pass": True,
        "literal_difference": bill_raw != metadata_raw,
        "compatibility_approved": False,
        "historical_bytes_repinned": False,
    }
