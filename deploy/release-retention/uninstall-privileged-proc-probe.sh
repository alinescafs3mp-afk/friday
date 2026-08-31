#!/bin/sh
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
fail() { printf '%s\n' friday_retention_probe_uninstall_failed >&2; exit 2; }
sync_file() { /usr/bin/sync -f "$1" || fail; }

ROOT_PREFIX=/
while [ "$#" -gt 0 ]; do
    case "$1" in
        --root) [ "$#" -ge 2 ] || fail; ROOT_PREFIX=$2; shift 2 ;;
        *) fail ;;
    esac
done
[ "${ROOT_PREFIX#/}" != "$ROOT_PREFIX" ] || fail
TEST_MODE=${FRIDAY_RETENTION_INSTALL_TEST_MODE:-0}
if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    [ "$ROOT_PREFIX" = / ] && [ "$TEST_MODE" = 0 ] || fail
    EXPECTED_UID=0 EXPECTED_GID=0
else
    [ "$TEST_MODE" = 1 ] && [ "$ROOT_PREFIX" != / ] || fail
    EXPECTED_UID=$(/usr/bin/id -u) EXPECTED_GID=$(/usr/bin/id -g)
fi

if [ "$ROOT_PREFIX" = / ]; then
    HELPER_DIR=/usr/libexec/friday SUDOERS_DIR=/etc/sudoers.d
else
    HELPER_DIR=$ROOT_PREFIX/usr/libexec/friday SUDOERS_DIR=$ROOT_PREFIX/etc/sudoers.d
fi
HELPER=$HELPER_DIR/release_artifact_proc_probe.py
SCOPE=$HELPER_DIR/release_artifact_proc_scope.v1.json
SUDOERS=$SUDOERS_DIR/friday-retention-probe
LOCK=$HELPER_DIR/.release-artifact-proc-probe.install.lock
SUDOERS_REMOVING=$SUDOERS_DIR/.friday-retention-probe.uninstalling

validate_directory() (
    directory=$1
    [ -d "$directory" ] && [ ! -L "$directory" ] || fail
    [ "$(/usr/bin/readlink -f "$directory")" = "$directory" ] || fail
    [ "$(/usr/bin/stat -c %u "$directory")" -eq "$EXPECTED_UID" ] || fail
    mode=$(/usr/bin/stat -c %a "$directory") || fail
    [ "$((0$mode & 0022))" -eq 0 ] || fail
)
validate_chain() (
    current=$1
    while :; do
        validate_directory "$current"
        [ "$current" = / ] && break
        current=$(/usr/bin/dirname -- "$current") || fail
    done
)
validate_file_if_present() (
    path=$1
    if [ -e "$path" ] || [ -L "$path" ]; then
        [ -f "$path" ] && [ ! -L "$path" ] || fail
        [ "$(/usr/bin/stat -c %u "$path")" -eq "$EXPECTED_UID" ] || fail
        [ "$(/usr/bin/stat -c %h "$path")" -eq 1 ] || fail
        mode=$(/usr/bin/stat -c %a "$path") || fail
        case "$mode" in 400|440|600|755) ;; *) fail ;; esac
    fi
)

if [ "$ROOT_PREFIX" = / ]; then
    validate_chain "$HELPER_DIR"
    validate_chain "$SUDOERS_DIR"
else
    validate_directory "$ROOT_PREFIX"
    validate_directory "$ROOT_PREFIX/usr"
    validate_directory "$ROOT_PREFIX/usr/libexec"
    validate_directory "$ROOT_PREFIX/etc"
    validate_directory "$HELPER_DIR"
    validate_directory "$SUDOERS_DIR"
fi
if [ -e "$LOCK" ] || [ -L "$LOCK" ]; then
    validate_file_if_present "$LOCK"
    [ "$(/usr/bin/stat -c %a "$LOCK")" = 600 ] || fail
else
    /usr/bin/install -o "$EXPECTED_UID" -g "$EXPECTED_GID" -m 0600 /dev/null "$LOCK" || fail
    sync_file "$HELPER_DIR"
fi
exec 9<>"$LOCK" || fail
/usr/bin/flock -n 9 || fail

# The grant is revoked first and is never republished by uninstall recovery.
validate_file_if_present "$SUDOERS"; validate_file_if_present "$SUDOERS_REMOVING"
if [ -e "$SUDOERS" ] || [ -L "$SUDOERS" ]; then
    if [ -e "$SUDOERS_REMOVING" ] || [ -L "$SUDOERS_REMOVING" ]; then
        /usr/bin/rm -f -- "$SUDOERS" || fail
    else
        /usr/bin/mv -- "$SUDOERS" "$SUDOERS_REMOVING" || fail
    fi
    sync_file "$SUDOERS_DIR"
fi

for artifact in \
    "$HELPER" \
    "$SCOPE" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.new" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.revoked" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.rollback" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.absent" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.uninstalling" \
    "$HELPER_DIR/.release_artifact_proc_scope.v1.json.new" \
    "$HELPER_DIR/.release_artifact_proc_scope.v1.json.revoked" \
    "$HELPER_DIR/.release-artifact-proc-probe.install.v1.json" \
    "$HELPER_DIR/.release-artifact-proc-probe.install.v1.json.new" \
    "$SUDOERS_DIR/.friday-retention-probe.new" \
    "$SUDOERS_DIR/.friday-retention-probe.revoked" \
    "$SUDOERS_DIR/.friday-retention-probe.rollback" \
    "$SUDOERS_DIR/.friday-retention-probe.absent" \
    "$SUDOERS_REMOVING"
do
    validate_file_if_present "$artifact"
done

/usr/bin/rm -f -- \
    "$HELPER" \
    "$SCOPE" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.new" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.revoked" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.rollback" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.absent" \
    "$HELPER_DIR/.release_artifact_proc_probe.py.uninstalling" \
    "$HELPER_DIR/.release_artifact_proc_scope.v1.json.new" \
    "$HELPER_DIR/.release_artifact_proc_scope.v1.json.revoked" \
    "$HELPER_DIR/.release-artifact-proc-probe.install.v1.json" \
    "$HELPER_DIR/.release-artifact-proc-probe.install.v1.json.new" || fail
sync_file "$HELPER_DIR"
/usr/bin/rm -f -- \
    "$SUDOERS_DIR/.friday-retention-probe.new" \
    "$SUDOERS_DIR/.friday-retention-probe.revoked" \
    "$SUDOERS_DIR/.friday-retention-probe.rollback" \
    "$SUDOERS_DIR/.friday-retention-probe.absent" \
    "$SUDOERS_REMOVING" || fail
sync_file "$SUDOERS_DIR"
printf '%s\n' friday_retention_probe_uninstalled
