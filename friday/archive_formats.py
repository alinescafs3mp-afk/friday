"""One closed archive-container dispatch shared by intake and extraction."""

from __future__ import annotations

from pathlib import Path

_COMPOUND_ARCHIVE_SUFFIXES = (".tar.bz2", ".tar.gz", ".tar.xz", ".tar.zst")
_ARCHIVE_SUFFIXES = frozenset({".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst"})
# A declared archive MIME is useful when Telegram has replaced an unsafe or
# credential-bearing name with ``.bin``.  It must not, however, demote a known
# semantic document to its implementation container: DOCX/ODF/EPUB are ZIPs,
# but their native parsers define what their contents mean.  Keep this closed
# list aligned with the explicit suffix dispatches in ``DocumentExtractor``;
# unknown/neutral carrier suffixes still use the MIME fallback below.
_NATIVE_DOCUMENT_SUFFIXES = frozenset(
    {
        ".bmp",
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".doc",
        ".docm",
        ".docx",
        ".dot",
        ".dotm",
        ".dotx",
        ".dps",
        ".dpt",
        ".eml",
        ".epub",
        ".et",
        ".ett",
        ".htm",
        ".html",
        ".ini",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".jsonl",
        ".key",
        ".log",
        ".markdown",
        ".md",
        ".mht",
        ".mhtml",
        ".msg",
        ".numbers",
        ".odg",
        ".odm",
        ".odp",
        ".ods",
        ".odt",
        ".otg",
        ".oth",
        ".otp",
        ".ots",
        ".ott",
        ".pages",
        ".pdf",
        ".png",
        ".pot",
        ".potm",
        ".potx",
        ".pps",
        ".ppsm",
        ".ppsx",
        ".ppt",
        ".pptm",
        ".pptx",
        ".ps1",
        ".pub",
        ".py",
        ".rst",
        ".rtf",
        ".sh",
        ".sql",
        ".sldm",
        ".sldx",
        ".tif",
        ".tiff",
        ".toml",
        ".ts",
        ".tsv",
        ".txt",
        ".vdx",
        ".vsd",
        ".vsdm",
        ".vsdx",
        ".vstx",
        ".webp",
        ".wpd",
        ".wpt",
        ".xls",
        ".xlsb",
        ".xlsm",
        ".xlt",
        ".xltm",
        ".xltx",
        ".xlsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_ARCHIVE_MIME_KINDS = {
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-bzip": ".bz2",
    "application/x-bzip2": ".bz2",
    "application/x-xz": ".xz",
    "application/zstd": ".zst",
    "application/x-zstd": ".zst",
}


def archive_dispatch_kind(filename: str, mime_type: str = "") -> str | None:
    """Return the extractor's archive kind from a closed suffix/MIME map.

    A recognised filename wins so compound tar compression remains available;
    the declared MIME is the fallback for transports which deliberately replace
    a credential-bearing archive name with a neutral ``.bin`` name.
    """

    safe_name = Path(str(filename or "")).name.casefold()
    for suffix in _COMPOUND_ARCHIVE_SUFFIXES:
        if safe_name.endswith(suffix):
            return suffix
    suffix = Path(safe_name).suffix.casefold()
    if suffix in _ARCHIVE_SUFFIXES:
        return suffix
    if suffix in _NATIVE_DOCUMENT_SUFFIXES:
        return None
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
    return _ARCHIVE_MIME_KINDS.get(normalized_mime)


__all__ = ["archive_dispatch_kind"]
