#!/usr/bin/env python3
"""Apply two exact GPT-OSS/ModelOpt compatibility edits inside the pinned image."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path("/sgl-workspace/sglang/python")
PATCH = Path("/tmp/friday-secondary-compat.patch")
PATCH_SHA256 = "0408f38a639c4a477e9ba14dacb488cb3d120fda0f4019b280fc999fa5fe0b5e"
EDITS = (
    (
        ROOT / "sglang/srt/layers/moe/fused_moe_triton/layer.py",
        "1aa9a454a3726476a6c52e8b2c28a2e0d1907d460c132b4c41cf481ad8a5884c",
        "4b38c1c3f86fa417d780ffe6da13101eaf84b8cec1a3f450df97494c39aeabb9",
        b"""        if (\n            self.quant_config is not None\n            and "modelopt" in self.quant_config.get_name()\n            and (expert_data.dim() != 2 or loaded_weight.dim() != 2)\n        ):\n            raise ValueError(\n                f"Expected 2D tensors, got expert_data shape {expert_data.shape} and loaded_weight shape {loaded_weight.shape}"\n            )\n\n""",
        b"",
    ),
    (
        ROOT / "sglang/srt/models/gpt_oss.py",
        "e66d25268dae3180eb8acf04247c512c5dc9465fd1f76473ae7f1c8c4bb4bb89",
        "f86edc233a716634d48630a6e8d3f85fd159da5cb090b2ba98233888474c0b34",
        b'            attention_bias=attention_bias,\n            prefix=add_prefix("self_attn", prefix),\n',
        b'            attention_bias=attention_bias,\n            quant_config=quant_config,\n            prefix=add_prefix("self_attn", prefix),\n',
    ),
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    if _digest(PATCH.read_bytes()) != PATCH_SHA256:
        raise RuntimeError("compatibility patch identity differs")
    for path, expected_before, expected_after, before, after in EDITS:
        source = path.read_bytes()
        if _digest(source) != expected_before or source.count(before) != 1:
            raise RuntimeError("upstream compatibility target differs")
        updated = source.replace(before, after, 1)
        if _digest(updated) != expected_after:
            raise RuntimeError("patched compatibility target differs")
        path.write_bytes(updated)
    print("friday secondary compatibility patch: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
