"""Bounded contract errors and identity predicates. No I/O."""

import re

DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA512_DIGEST = re.compile(r"^[0-9a-f]{128}$")
FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")

MAX_DEPTH = 8
META_SCHEMA_DEPTH = 16
DOCUMENT_DEPTH_LIMIT = 12
MAX_STRING = 512
MAX_ITEMS = 512
ADDITIONAL_MAX = 128
MAX_DOCUMENT_BYTES = 2000000
MAX_INRELEASE_BYTES = 2000000
MAX_PACKAGES_BYTES = 80000000
MAX_ACTIVE_SLOTS = 128
MAX_WHOLE_READ_BYTES = 512 * MAX_PACKAGES_BYTES
MAX_OUTPUT_BYTES = 33554432
MAX_FIELD_VALUE = 4000000
MAX_FIELD_NAME = 64
MAX_STANZAS = 200000
MAX_FIELDS = 64
MAX_CLEARSIGN_BYTES = 2000000
MAX_MEMBER_SIZE = 2000000000
MAX_INTEGER_DIGITS = 10


_STAGE = ["ingress"]


class ContractError(Exception):
    def __init__(self, cause, stage=None):
        super().__init__(cause)
        self.cause = cause
        self.stage = _STAGE[0] if stage is None else stage


def is_digest(value):
    return type(value) is str and DIGEST.fullmatch(value) is not None


def is_sha512(value):
    return type(value) is str and SHA512_DIGEST.fullmatch(value) is not None


def is_fingerprint(value):
    return type(value) is str and FINGERPRINT.fullmatch(value) is not None


def is_commit(value):
    return type(value) is str and COMMIT.fullmatch(value) is not None


def require_digest(value, cause):
    if not is_digest(value):
        raise ContractError(cause)
    return value


def positive_int(value, limit, cause):
    if type(value) is not int or isinstance(value, bool):
        raise ContractError(cause)
    if value < 1 or value > limit:
        raise ContractError(cause)
    return value


def bounded_int(text, limit, cause):
    if type(text) is bytes:
        try:
            text = text.decode("ascii")
        except UnicodeError as exc:
            raise ContractError(cause) from exc
    if type(text) is not str or not text.isdigit() or len(text) > MAX_INTEGER_DIGITS:
        raise ContractError(cause)
    value = int(text)
    if value < 0 or value > limit:
        raise ContractError(cause)
    return value
