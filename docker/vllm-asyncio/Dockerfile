FROM vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089

# vLLM 0.25.1 starts its single-process HTTP API with uvloop.run().  On the
# pinned Python 3.12 / uvloop 0.22.1 runtime that path can fail during timer
# cleanup.  Preserve the serving stack and replace only the reviewed top-level
# runner.  The patch is fail-closed and verifies the exact upstream file hash.
COPY docker/vllm-asyncio/patch_serve.py /usr/local/share/friday/patch_vllm_serve.py
RUN ["python3", "/usr/local/share/friday/patch_vllm_serve.py", "/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/cli/serve.py"]
