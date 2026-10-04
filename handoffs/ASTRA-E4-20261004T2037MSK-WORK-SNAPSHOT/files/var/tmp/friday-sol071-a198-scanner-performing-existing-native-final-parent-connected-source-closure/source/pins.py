"""Identity pins for the source handoff. These hashes are not publisher proof."""

LAB818_INPUT_PATH = (
    "/home/jericho/.jericho/grok-takeover/"
    "ASTRA-E4-LAB818-RETAINED-ARCHIVE-MEMBER-INVENTORY-SOURCE-INPUT.json"
)
LAB818_INPUT_SHA256 = "e98e9c98a48ea14bf5ab887cc8ded9d4aed95ea50e1e0cf9b16b7c722993bdf9"
LAB818_INPUT_SIZE = 7752
LAB821_INPUT_PATH = (
    "/home/jericho/.jericho/grok-takeover/"
    "ASTRA-E4-LAB821-RETAINED-ARCHIVE-WHOLE-SOURCE-CORRECTION-INPUT.json"
)
LAB821_INPUT_SHA256 = "129cad010e168e038c43139ce9e2603dec6ba6928440362c2a9286bc989befad"
LAB821_INPUT_SIZE = 11524
INPUT_PATH = LAB821_INPUT_PATH
INPUT_SHA256 = LAB821_INPUT_SHA256
INPUT_SIZE = LAB821_INPUT_SIZE
RETAINED_INDEX_SHA256 = "41781d9297176e57c5b222a3692c737e8953cae175eec17e2477af9a48a31197"
RETAINED_INDEX_SIZE = 238293
LAB818_RESULT_SHA256 = "bffd29f61f78b0576801ab11f347fb30ea09a65c0f922030b657381717853954"
LAB818_MANIFEST_SHA256 = "9c7ab2b4feb4268b69cd67b29b5f80c4d35bd7a5b87c0f679c9403a6d26b0447"
A053_RESULT_SHA256 = "ced5c2cdd65bd3002119aca4d2a99e24f1c3e21b1c2d1d98adfec8664dc98dde"
A053_COVERAGE_SHA256 = "7e707e5ba641e5d59f8cb926190178d1a0493dd51a61960707880b766e7e2cfe"
A053_DECISION_SHA256 = "9f4dbc65320d763cc868be8be6a5fec13a2a89309cfefb7d0f2d697522a65f24"

BILL_PATH = (
    "/home/jericho/.jericho/runtime/subagent-lifecycle/"
    "ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032-G1-BILL.json"
)
BILL_SHA256 = "cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2"
BILL_SIZE = 466956

INVENTORY_PATH = (
    "/home/jericho/.jericho/runtime/subagent-lifecycle/"
    "ASTRA-E4-PARTIAL-ACQUISITION-CUSTODY-BROWSER-SUCCESSOR-A035-G1-PARTIAL-INVENTORY.json"
)
INVENTORY_SHA256 = "90f2076b1368b56b563d2eeba146fee8c51d76d541ac5b43864487034f1f554d"
INVENTORY_SIZE = 96626

ADOPTION_SHA256 = "189b6e1f38fc252e5afec4a7197699196ac400dcafa8aa0aabe1b8ff5e15a696"
UBUNTU_CHAIN_SHA256 = "b24430e0849afcb8be8f2dfc97309266069d73601e7b3315b7c0a89e7d827fd1"
KERNEL2_SHA256 = "f32f183db867624100c539979a987b7074a3c0110977f65104ab524e6544ed6e"
PYPI94_SHA256 = "24e5c82c39d7cec839feb23745a276f6c515ddc3fbfe053c6c780a833bec47ae"
SOL021_PACKAGE_SHA256 = "e31547c3b6f86430eac6f9279808fe21289b40cc8f8fdaa6a3b9a7e4ac08308f"
A009_SHA256 = "4020a7121dc4a23c0d98700c3495172feafef4e191b4a4eb0f57363349e0904f"
NO_COMPACT_SHA256 = "d116094ca40bcdebe30c41146642f4510bb52b628e09e314fa6f7ce1396333a7"
MAXIMUM_USE_SHA256 = "c3ba47fd6aaf47d0c159650e0989316cc6fa7bcdb65869e4e2981d82bd9f13c4"

HELD_ROOT = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
POSITIVE_WHEELS = 94
POSITIVE_UBUNTU = 106
POSITIVE_KERNEL = 2
POSITIVE_TOTAL = 202

# Bill raw Requires-Python values that must not be collapsed into each other.
TEXTTABLE_REQUIRES_PYTHON_RAW = ""
BROTLI_REQUIRES_PYTHON_RAW = None

PYPI94_DECISION = "OBSERVED_94_EXACT_ARCHIVE_IDENTITIES_ONLY_FULL_AUDIT_ADMISSION_NOT_PROVEN"
KERNEL2_DECISION = "ADOPT_QUALIFIED_EXACT_CURRENT_SIGNED_METADATA_KERNEL2_ARCHIVE_IDENTITY_LINK_ONLY"

SNAPSHOT_SCHEMA = "snapshot-manifest.v1"
PROVENANCE_SCHEMA = "material-provenance.v1"

SNAPSHOT_KEYS = (
    "schema",
    "contract",
    "snapshot_id",
    "creation_tool_sha256",
    "created_utc",
    "candidate",
    "platform",
    "materials_sha256",
    "runtime_contract_sha256",
    "members",
    "root_merkle_sha256",
)

PROVENANCE_KEYS = (
    "schema",
    "manifest_sha256",
    "assembly_recipe_sha256",
    "creation_tool_sha256",
    "approved_authorities",
    "artifacts",
    "owner_approval",
)

CODEC_PINS = {
    "deflate": {
        "implementation": "stdlib.zlib",
        "capability_sha256": "7ab4f76239445a27b76ad85832b467bfaef433ba974280615f0f44d18e06fd3f",
    },
    "gzip": {
        "implementation": "stdlib.zlib",
        "capability_sha256": "7fae18b513a8d706ed971c9458b4f71258f75cac14dbac90e82df249a654b9ee",
    },
    "xz": {
        "implementation": "stdlib.lzma",
        "capability_sha256": "0de12deb52d0e110d4663e43336d0d9393e8d283f4d947c18b7c5923dc86db53",
    },
    "zstd": {
        "implementation": "stdlib.compression.zstd",
        "capability_sha256": "fcd29cf9c53fd4db2af1c6253331042a0540fa67ec9e94cf82e99476319e16df",
    },
}

S21_07_ID = "CORRECT-S21-07"
S21_07_PRESERVE = (
    "Empty Requires-Python normalized to null is an explicit normalization. "
    "Immutable input identities are not publisher, held-byte, or runtime proof."
)

SUPPORTED_METADATA_VERSIONS = frozenset(("1.0", "1.1", "1.2", "2.1", "2.2", "2.3", "2.4"))
FUTURE_PLAN_SHA256 = "083fd2a22e468ab2adcced2a55e33225a6785b1c5dcb0362f139731c28b8f59c"
