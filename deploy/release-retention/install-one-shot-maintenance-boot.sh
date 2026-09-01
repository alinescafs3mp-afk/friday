#!/bin/sh
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

fail() {
    printf '%s\n' friday_retention_maintenance_install_failed >&2
    exit 2
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || fail
REPOSITORY=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P) || fail
HELPER=$REPOSITORY/tools/release_artifact_retention_maintenance_install.py
[ -f "$HELPER" ] && [ ! -L "$HELPER" ] || fail
[ "$(stat -c %h "$HELPER")" -eq 1 ] || fail

exec /usr/bin/python3 -I -B -S "$HELPER" install "$@"
