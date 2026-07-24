# Model directory

Place the complete local `qwen3.6-35b-a3b-nvfp4` model snapshot in this directory.
Model weights are deliberately not included in the source archive.

The default vLLM profile serves this directory as model name `dispatcher` with a
32,768-token context, fp8 KV cache, multimodal limits, and the retained runtime
parameters documented in the root README.
