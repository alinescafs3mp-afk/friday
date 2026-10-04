"""Independent pins. Absent proofs stay None and do not become success."""
BILL_SHA256 = '091a5f7d41d197e6f9e561bc7afb186b23a1c3ea9a3db224c7f660a01a095c7e'
HISTORICAL_UBUNTU_IDENTITY_SHA256 = '777d43a8dca8be6260f4f555303a14c7502c5f00b3db5d1b0d91fb53c1937a87'
HISTORICAL_WHEEL_IDENTITY_SHA256 = 'b520b87014837103e96a36907d596194a9d5b59ffeab438fc5049053be9e2b1f'
UBUNTU_PROJECTION_SHA256 = '7db113112b86a63a5903ed61d8e4fb967ca093e0a3e183afbcabb0544ccc2792'
WHEEL_PROJECTION_SHA256 = '638c72becb37977c3296c4b86f688e266848b9d7d0868e43d8fca4e06d9f998b'
INDEX_PROJECTION_SHA256 = 'f57ce7da486593112883a2ce0613c5710e084fa2f4c1f633f2321756d57f87ab'
A009_SHA256 = '4020a7121dc4a23c0d98700c3495172feafef4e191b4a4eb0f57363349e0904f'
A032_BILL_SHA256 = 'cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2'
A033_GPGV_SHA256 = '95ecd00d02b79d091f103af175b1dbf95b5b0c66503104bfcea447cca7e3829d'
NO_COMPACT_SHA256 = 'd116094ca40bcdebe30c41146642f4510bb52b628e09e314fa6f7ce1396333a7'
CANDIDATE_COMMIT = 'cecd28a92ac4fd4e34c4d3813d0c598debe09436'
CANDIDATE_TREE = 'e7e86c7796a3db6571ffea715e666247f8876d63'
GOLDEN_COMMIT = '5497d28dedf49b219fcc2112d6b391ba6cbbc690'
UBUNTU_FINGERPRINT = 'F6ECB3762474EDA9D21B7022871920D1991BC93C'
UBUNTU_KEYRING_SHA256 = '80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31'
UBUNTU_EXECUTABLE = '/usr/bin/gpgv'
UBUNTU_ISSUER_ID = 'ubuntu-archive'
WHEEL_ISSUER_ID = 'pypi-retained'
NODE_ISSUER_ID = 'nodejs.org'
RESOLUTE_UPDATES_INRELEASE_HISTORICAL = 'c73c04e538b29eedbb569e62179477edaef4ff3751abf243dc58784c340b2f18'
RESOLUTE_UPDATES_INRELEASE_OBSERVED = '802e675dd9de4c7f3916434a95e7c1d8eec0e82886622d7805ab19a2c6fe0365'
NODE_FILENAME = 'node-v22.23.2-linux-x64.tar.xz'
NODE_SIZE = 31058332
ORIGINAL_ARTIFACT_MAX = 350
ORIGINAL_SNAPSHOT_MEMBER_MAX = 512
ORIGINAL_STREAM_SLOTS = 128
UBUNTU_PACKAGES_DECLARED_TOTAL_BYTES = 101554111
UBUNTU_PACKAGES_LARGEST_DECLARED_BYTES = 76792699
NODE_VERSION = '22.23.2'
NODE_DIAGNOSTIC_SHA256 = 'd60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307'
NODE_STATED_FINGERPRINT_UNVERIFIED = 'CC68F5A3106FF448322E48ED27F5E38D5B0A215F'
NODESOURCE_FINGERPRINT_NOT_AUTHORITY = '6F71F525282841EEDAF851B42F59B5F99B1BE0B4'
UNRAR_VERSION = '7.20'
UNRAR_STATUS = 'BLOCKED_PUBLISHER_GAP'
PLAYWRIGHT_VERSION = '1.61.0'
CHROMIUM_REVISION = '1228'
HEADLESS_REVISION = '1228'
FFMPEG_REVISION = '1011'
PLATFORM = 'ubuntu-26.04-x86-64'
ARCHITECTURE = 'amd64'
PYTHON_VERSION = '3.14.4'
PYTHON_ABI = 'cp314-regular'
UBUNTU_ARGV = (
    "/usr/bin/gpgv",
    "--keyring",
    "/usr/share/keyrings/ubuntu-archive-keyring.gpg",
)
NODE_AUTHORITATIVE_SHA256 = None
NODE_SIGNER_SHA256 = None
ROOT_ISSUER_SHA256 = None
UNRAR_PUBLISHER_DIGEST = None
UBUNTU_MINIMUM_COUNT = 106
WHEEL_COUNT = 94
INDEX_COUNT = 13
OPERATION_COUNT = 15
ORIGINAL_OBLIGATION_CONCEPTS = 32
LEGACY_LITERAL_IDS = 31
CONTROL_COUNT = 69
PRODUCER_MAY_MINT = False
UNSUPPORTED_HASH_ALIAS = {
    "unsupported_hash": "clearsign_unknown_hash",
}
TUPLE_MIGRATIONS = {
    "unsupported_hash": {
        "current_id": "clearsign_unknown_hash",
        "original_id": "clearsign_unknown_hash_not_proven",
        "original_scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN",
        "original_status": "NOT_PROVEN",
        "current_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
        "current_status": "REFUSED",
        "current_cause": "unsupported_hash",
    },
    "clearsign_unknown_hash_not_proven": {
        "current_id": "clearsign_unknown_hash",
        "original_id": "clearsign_unknown_hash_not_proven",
        "original_scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN",
        "original_status": "NOT_PROVEN",
        "current_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
        "current_status": "REFUSED",
        "current_cause": "unsupported_hash",
    },
    "ubuntu_tool_flag_before_attestation": {
        "current_id": "ubuntu_tool_flag_before_attestation",
        "original_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
        "current_scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE",
    },
    "ubuntu_candidate_signer_refused": {
        "current_id": "ubuntu_candidate_signer_refused",
        "original_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
        "current_scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE",
    },
    "clearsign_sha512_structural": {
        "current_id": "clearsign_sha512_structural",
        "original_status": "SUPPORTED",
        "current_status": "ACCEPTED",
    },
    "packages_unknown_field_kept": {
        "current_id": "packages_unknown_field_kept",
        "original_scenario": "MATERIAL_PLATFORM_COMPATIBILITY",
        "current_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
    },
    "recipe_closed_effects_refused": {
        "current_id": "recipe_closed_effects_refused",
        "original_status": "RECIPE_CLOSED_EFFECTS_REFUSED",
        "current_status": "PLANNED_EFFECTS_UNAVAILABLE",
    },
    "recipe_image_sha_not_mandatory": {
        "current_id": "recipe_image_sha_not_mandatory",
        "original_status": "RECIPE_CLOSED_EFFECTS_REFUSED",
        "current_status": "PLANNED_EFFECTS_UNAVAILABLE",
    },
    "unrar_gap_explicit": {
        "current_id": "unrar_gap_explicit",
        "original_scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE",
        "current_scenario": "ROOTFS_NATIVE_DATA_RECIPE",
    },
    "browser_attribution_not_proven": {
        "current_id": "browser_attribution_not_proven",
        "original_scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE",
        "current_scenario": "ROOTFS_NATIVE_DATA_RECIPE",
    },
    "expected_digest_not_replaced": {
        "current_id": "expected_digest_not_replaced",
        "original_scenario": "INPUT_AND_EXTERNAL_AUTHORITY",
        "current_scenario": "UBUNTU_RAW_SIGNATURE_CHAIN",
    },
}
EXPECTED_FIXTURE_SHA256 = 'c5ca5eb01f54016d54b8a2272d77a43d5bce344743c3b35e190aa529c0c6bffb'
AUTHORITY_FIXTURE_SHA256 = '6985a2ad48db99c01b0ef6b9fee3f46a291736023e17da19159e24ef66684c31'
PRESENTED_FIXTURE_SHA256 = 'e35ee2e1a06d5bb75a7e6ff6cea6baf2b043bde6328ca2267b699f3f22cadf91'
HISTORICAL_SCHEMA_PINS = {
    "effect-capability.v1": "08f268038d6f11e8b959fb611de508f177a24d7dc63d38bca5e03837eae44aaf",
    "external-issuer-approval.v1": "ced655df40b9d42fdbd28252ce69b81396b6e50beec3b4a086d0090b889233b6",
    "material-provenance.v1": "2e184d6ed70bb5cee67f0142eb6dbf6e6b396099f790333f32b10f8bea2b3017",
    "producer-consumer-binding.v1": "468363921c5e417117029724b5e57212fe29c738a923f7ce8534898bb6f0a7ad",
    "raw-publisher-receipt.v1": "3a990e593be2a0f7bf1fc2952c8dfd8b31fc30f889da31b7d8e6bedb0d9b6eed",
    "rootfs-construction-recipe.v1": "4cf98023c845f398780cc5b5bc94ebed08ab24d7fb6f260279ec7064c21fa0be",
    "snapshot-manifest.v1": "7b9191fd02a032297ad7c91626c0736752c9133216b366e950fca5af2431ce24",
    "trusted-root-projection.v1": "59c1c9842144a7fba17bb5f2fdc840dbdbd7b280df88ebf9ab3b1abdf34d08fa",
    "verification-result.v1": "9edce02600bb9ea013bef30eab260d50749f8634f072b6d45466a85920bdc560",
    "verifier-capability.v1": "35f9c73cff80743a3754eff39f018317a37baac69921da13ff3273e1f1b2cc21"
}
SCHEMA_PINS = {
    "effect-capability.v1": "df1ffec408e70975cc58db061244e401741f878d5708745c472b2c64bf6e5d28",
    "external-issuer-approval.v1": "51d9f5d39a77cab459b20a4bdae8e882a34a8d69c95c040928cf9529ed79f2c6",
    "friday.lab820.approval-result.v1": "d2f5a56e40161fe5105c7e3d8ca1265fea2a38c11d42c7f08a8922106df4760d",
    "friday.lab820.authority-ingress.v1": "e2dc4586cdc3f28294b5ad4fde0aa646de235a6c3ab27797b1d4c913536948b1",
    "friday.lab820.evidence.v1": "0bee9250a87028dc825587a01a81c2604d6c0d84a58fff97d59a468719b50d12",
    "friday.lab820.expected-bill.v1": "9b0dcff71f76a0be8c27df3348ed6f26a35ca8efa05ecfc3df668df0034556f9",
    "friday.lab820.node-authoritative.v1": "3b6a85b1c1df5ee7bda72d5696fed98fc57984b9f21902eef0268976ba6c9ec5",
    "friday.lab820.package-row.v1": "37c67ee85c58d68b10f313cb03e98bc81f90c53ece15ba956e884cfd4c3f343c",
    "friday.lab820.presented-observation.v1": "850e0bcb7c2ee6644af4294774602c833940a97ddcfad7546d06c6f6d42f33e5",
    "friday.lab820.release-member.v1": "26ecf48102d33ccd2c933f236cde8d23905f93d1785b3001f6f1a3c493112b53",
    "friday.lab820.resource.v1": "92fcb73114a38a3c4bca58877615b24dc8f932bc8869ab1d239c6df7c6cdbe98",
    "friday.lab824.independent-trust-context.v1": "563343895326b3f0f89d7f1381225398fa27074ba406f7c2a47fb4c31c02ed36",
    "material-provenance.v1": "883b493876cc2842a303ce3286989fd02cc545e20a671222b540b08c8626725c",
    "producer-consumer-binding.v1": "9680069f44ba9eeaa12c8cae0a38f6956ca4950abf2f99ea7b5cca09f1bbcd82",
    "raw-publisher-receipt.v1": "1fe2fac28b0736f21a7638f9bce362d3fb67fe7a80c4b6b2e8ee4f823005b5ee",
    "rootfs-construction-recipe.v1": "ab10052e9a6dccebf715ac80321f725d71d268d1bc6324cc3ee165c1e3129e7b",
    "snapshot-manifest.v1": "219f59cb178e17b4cd311afc65d1a8444775a6ecd5e16eae37878d1a3f633b72",
    "trusted-root-projection.v1": "c7580eef2cd7fd255ccf013670784b140d826841154a3d9b6e9dc7b4e00351c3",
    "verification-result.v1": "c91bd65d4676c9355f7516b5aee0e76e06a88771af2a4b53acff44b65aa3ff06",
    "verifier-capability.v1": "e7d2d438839d74274ae40df09ce11e0551ae947ff1b9372effc394790f7b1717"
}
