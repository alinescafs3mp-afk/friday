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

exec "$@"
