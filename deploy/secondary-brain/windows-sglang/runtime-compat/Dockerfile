ARG SOURCE_DATE_EPOCH=0
FROM lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738

COPY --chmod=0444 compat.patch /tmp/friday-secondary-compat.patch
COPY --chmod=0555 apply_compat.py /tmp/friday-apply-compat.py
RUN python3 -I -B /tmp/friday-apply-compat.py \
    && python3 -m py_compile \
        /sgl-workspace/sglang/python/sglang/srt/layers/moe/fused_moe_triton/layer.py \
        /sgl-workspace/sglang/python/sglang/srt/models/gpt_oss.py \
    && rm -rf \
        /sgl-workspace/sglang/python/sglang/srt/layers/moe/fused_moe_triton/__pycache__ \
        /sgl-workspace/sglang/python/sglang/srt/models/__pycache__ \
        /tmp/friday-apply-compat.py

LABEL com.friday.secondary.compat.base-image-ref="lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738" \
      com.friday.secondary.compat.patch-sha256="0408f38a639c4a477e9ba14dacb488cb3d120fda0f4019b280fc999fa5fe0b5e" \
      com.friday.secondary.compat.applier-sha256="67182abfc5104facbf870af7ebd2a108445b2ace7e3da9194b9586ffa8b83726" \
      com.friday.secondary.compat.layer-original-sha256="1aa9a454a3726476a6c52e8b2c28a2e0d1907d460c132b4c41cf481ad8a5884c" \
      com.friday.secondary.compat.layer-patched-sha256="4b38c1c3f86fa417d780ffe6da13101eaf84b8cec1a3f450df97494c39aeabb9" \
      com.friday.secondary.compat.gpt-oss-original-sha256="e66d25268dae3180eb8acf04247c512c5dc9465fd1f76473ae7f1c8c4bb4bb89" \
      com.friday.secondary.compat.gpt-oss-patched-sha256="f86edc233a716634d48630a6e8d3f85fd159da5cb090b2ba98233888474c0b34"
