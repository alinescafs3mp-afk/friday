#!/bin/sh
set -eu

mkdir -p \
  /runtime/data/state \
  /runtime/data/files \
  /runtime/data/memory-vault \
  /runtime/data/backups \
  /runtime/data/exports \
  /runtime/cache \
  /runtime/logs \
  /runtime/models/qwen3.6-27b-nvfp4-nvidia

# The backend deliberately refuses a permissive Obsidian root.  Compose owns
# this exact path, so establish its invariant before configuration validation.
install -d -m 0700 \
  /runtime/data/obsidian \
  /runtime/data/obsidian/profiles \
  /runtime/data/obsidian/run \
  /runtime/data/obsidian/logs \
  /runtime/data/obsidian/vaults

exec "$@"
