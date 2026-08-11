"""Storage-safe encoding for strings in the closed technical-metadata schema.

The generic Raw-object privacy gate deliberately rejects JSON string leaves
that look like nested JSON.  Technical metadata, however, may legitimately
start with ``[``, ``{`` or a quote.  Only the closed ingestion projection uses
this codec; it is not an escape hatch for arbitrary metadata.
"""

from __future__ import annotations

TECHNICAL_METADATA_TEXT_CODEC_FIELD = "technical_metadata_text_codec"
TECHNICAL_METADATA_TEXT_CODEC_VERSION = 1
TECHNICAL_METADATA_SCHEMA_VERSION = 4
EMPTY_TECHNICAL_METADATA_VALUE = "(пустое значение)"

_TECHNICAL_METADATA_TEXT_PREFIX = "technical-metadata-text-v1:"
_RISKY_JSON_PREFIXES = ("{", "[", '"')


def encode_technical_metadata_text(value: str) -> str:
    """Encode only values that the generic nested-JSON gate must reject.

    Prefix-shaped literal values are encoded too, keeping the representation
    reversible once the enclosing projection carries the codec version.
    """

    if not (
        value.lstrip().startswith(_RISKY_JSON_PREFIXES) or value.startswith(_TECHNICAL_METADATA_TEXT_PREFIX)
    ):
        return value
    return f"{_TECHNICAL_METADATA_TEXT_PREFIX}{value}"


def decode_technical_metadata_text(value: str) -> tuple[str, bool]:
    """Return decoded text and whether a prefixed representation was valid."""

    if not value.startswith(_TECHNICAL_METADATA_TEXT_PREFIX):
        return value, True
    decoded = value.removeprefix(_TECHNICAL_METADATA_TEXT_PREFIX)
    if encode_technical_metadata_text(decoded) != value:
        return "", False
    return decoded, True
