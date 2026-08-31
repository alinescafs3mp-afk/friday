#!/bin/sh
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

TEMPLATE_SHA256=502f46540eb5204618d93805eeece595d2f2a96c09c8f014aadfdfc524f7867b
fail() { printf '%s\n' friday_retention_probe_install_failed >&2; exit 2; }
digest() {
    value=$(/usr/bin/sha256sum -- "$1" | /usr/bin/cut -d ' ' -f 1) || fail
    [ "${#value}" -eq 64 ] || fail
    case "$value" in *[!0-9a-f]*) fail ;; esac
    printf '%s\n' "$value"
}
sync_file() { /usr/bin/sync -f "$1" || fail; }

OWNER_USER= SOURCE= EXPECTED_SHA256= ROOT_PREFIX=/
while [ "$#" -gt 0 ]; do
    case "$1" in
        --owner-user) [ "$#" -ge 2 ] || fail; OWNER_USER=$2; shift 2 ;;
        --source) [ "$#" -ge 2 ] || fail; SOURCE=$2; shift 2 ;;
        --expected-sha256) [ "$#" -ge 2 ] || fail; EXPECTED_SHA256=$2; shift 2 ;;
        --root) [ "$#" -ge 2 ] || fail; ROOT_PREFIX=$2; shift 2 ;;
        *) fail ;;
    esac
done
case "$OWNER_USER" in ''|*[!A-Za-z0-9_.-]*) fail ;; esac
[ "$OWNER_USER" != ALL ] && [ "$OWNER_USER" != root ] || fail
[ "${#EXPECTED_SHA256}" -eq 64 ] || fail
case "$EXPECTED_SHA256" in *[!0-9a-f]*) fail ;; esac
[ -n "$SOURCE" ] && [ "${SOURCE#/}" != "$SOURCE" ] || fail
[ "${ROOT_PREFIX#/}" != "$ROOT_PREFIX" ] || fail

TEST_MODE=${FRIDAY_RETENTION_INSTALL_TEST_MODE:-0}
if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    [ "$ROOT_PREFIX" = / ] && [ "$TEST_MODE" = 0 ] || fail
    EXPECTED_UID=0 EXPECTED_GID=0
else
    [ "$TEST_MODE" = 1 ] && [ "$ROOT_PREFIX" != / ] || fail
    EXPECTED_UID=$(/usr/bin/id -u)
    EXPECTED_GID=$(/usr/bin/id -g)
fi
OWNER_RECORD=$(/usr/bin/getent passwd "$OWNER_USER") || fail
[ "$(/usr/bin/printf '%s\n' "$OWNER_RECORD" | /usr/bin/wc -l)" -eq 1 ] || fail
OWNER_UID=$(/usr/bin/printf '%s\n' "$OWNER_RECORD" | /usr/bin/cut -d: -f3) || fail
case "$OWNER_UID" in ''|*[!0-9]*) fail ;; esac
[ "$OWNER_UID" -gt 0 ] || fail

validate_directory() (
    directory=$1 owner=$2
    [ -d "$directory" ] && [ ! -L "$directory" ] || fail
    [ "$(/usr/bin/readlink -f "$directory")" = "$directory" ] || fail
    [ "$(/usr/bin/stat -c %u "$directory")" -eq "$owner" ] || fail
    mode=$(/usr/bin/stat -c %a "$directory") || fail
    [ "$((0$mode & 0022))" -eq 0 ] || fail
)
validate_chain() (
    current=$1 owner=$2
    while :; do
        validate_directory "$current" "$owner"
        [ "$current" = / ] && break
        current=$(/usr/bin/dirname -- "$current") || fail
    done
)
validate_file() (
    path=$1 mode=$2
    [ -f "$path" ] && [ ! -L "$path" ] || fail
    [ "$(/usr/bin/stat -c %u "$path")" -eq "$EXPECTED_UID" ] || fail
    [ "$(/usr/bin/stat -c %a "$path")" = "$mode" ] || fail
    [ "$(/usr/bin/stat -c %h "$path")" -eq 1 ] || fail
)
ensure_directory() {
    directory=$1
    parent=$(/usr/bin/dirname -- "$directory") || fail
    validate_directory "$parent" "$EXPECTED_UID"
    if [ ! -e "$directory" ] && [ ! -L "$directory" ]; then
        /usr/bin/install -d -o "$EXPECTED_UID" -g "$EXPECTED_GID" -m 0755 "$directory" || fail
        sync_file "$parent"
    fi
    validate_directory "$directory" "$EXPECTED_UID"
}

PYTHON=$(/usr/bin/readlink -f /usr/bin/python3) || fail
case "$PYTHON" in /usr/bin/python3.[0-9]*) ;; *) fail ;; esac
validate_chain /usr/bin 0
[ -f "$PYTHON" ] && [ ! -L "$PYTHON" ] || fail
[ "$(/usr/bin/stat -c %u "$PYTHON")" -eq 0 ] || fail
mode=$(/usr/bin/stat -c %a "$PYTHON") || fail
[ "$((0$mode & 0022))" -eq 0 ] || fail
[ "$(/usr/bin/stat -c %h "$PYTHON")" -eq 1 ] || fail

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /usr/bin/pwd -P) || fail
TEMPLATE=$SCRIPT_DIR/friday-retention-probe.sudoers.in
[ -f "$TEMPLATE" ] && [ ! -L "$TEMPLATE" ] || fail
[ "$(digest "$TEMPLATE")" = "$TEMPLATE_SHA256" ] || fail
[ -f "$SOURCE" ] && [ ! -L "$SOURCE" ] || fail
[ "$(digest "$SOURCE")" = "$EXPECTED_SHA256" ] || fail

if [ "$ROOT_PREFIX" = / ]; then
    validate_chain /usr 0
    validate_chain /etc 0
    ensure_directory /usr/libexec
    HELPER_DIR=/usr/libexec/friday SUDOERS_DIR=/etc/sudoers.d
else
    validate_directory "$ROOT_PREFIX" "$EXPECTED_UID"
    ensure_directory "$ROOT_PREFIX/usr"
    ensure_directory "$ROOT_PREFIX/usr/libexec"
    ensure_directory "$ROOT_PREFIX/etc"
    HELPER_DIR=$ROOT_PREFIX/usr/libexec/friday SUDOERS_DIR=$ROOT_PREFIX/etc/sudoers.d
fi
ensure_directory "$HELPER_DIR"
ensure_directory "$SUDOERS_DIR"

HELPER=$HELPER_DIR/release_artifact_proc_probe.py
SCOPE=$HELPER_DIR/release_artifact_proc_scope.v1.json
SUDOERS=$SUDOERS_DIR/friday-retention-probe
LOCK=$HELPER_DIR/.release-artifact-proc-probe.install.lock
MANIFEST=$HELPER_DIR/.release-artifact-proc-probe.install.v1.json
MANIFEST_STAGE=$HELPER_DIR/.release-artifact-proc-probe.install.v1.json.new
HELPER_STAGE=$HELPER_DIR/.release_artifact_proc_probe.py.new
HELPER_REVOKED=$HELPER_DIR/.release_artifact_proc_probe.py.revoked
SCOPE_STAGE=$HELPER_DIR/.release_artifact_proc_scope.v1.json.new
SCOPE_REVOKED=$HELPER_DIR/.release_artifact_proc_scope.v1.json.revoked
SUDOERS_STAGE=$SUDOERS_DIR/.friday-retention-probe.new
SUDOERS_REVOKED=$SUDOERS_DIR/.friday-retention-probe.revoked
SUDOERS_REMOVING=$SUDOERS_DIR/.friday-retention-probe.uninstalling
if [ -e "$LOCK" ] || [ -L "$LOCK" ]; then
    validate_file "$LOCK" 600
else
    /usr/bin/install -o "$EXPECTED_UID" -g "$EXPECTED_GID" -m 0600 /dev/null "$LOCK" || fail
    sync_file "$HELPER_DIR"
fi
exec 9<>"$LOCK" || fail
/usr/bin/flock -n 9 || fail
[ ! -e "$SUDOERS_REMOVING" ] && [ ! -L "$SUDOERS_REMOVING" ] || fail

POLICY_SHA256=$(/usr/bin/printf '#%s ALL=(root) NOPASSWD: %s -I -B -S /usr/libexec/friday/release_artifact_proc_probe.py privileged-target-probe\n' "$OWNER_UID" "$PYTHON" | /usr/bin/sha256sum | /usr/bin/cut -d ' ' -f 1) || fail
SCOPE_BODY='{"required_capabilities":["CAP_SYS_ADMIN","CAP_SYS_PTRACE"],"schema":"friday.release-artifact-proc-host-scope.v1","scope":"initial_pid_namespace_and_proc_v1"}'
SCOPE_SHA256=$(/usr/bin/printf '%s\n' "$SCOPE_BODY" | /usr/bin/sha256sum | /usr/bin/cut -d ' ' -f 1) || fail
PHASE= OLD_HELPER_SHA256=none OLD_SCOPE_SHA256=none OLD_POLICY_SHA256=none
write_manifest() {
    phase=$1
    /usr/bin/printf '{"expected_helper_sha256":"%s","expected_policy_sha256":"%s","expected_scope_sha256":"%s","old_helper_sha256":"%s","old_policy_sha256":"%s","old_scope_sha256":"%s","owner":"%s","owner_uid":%s,"phase":"%s","python":"%s","schema":"friday.retention-probe-install.v1","template_sha256":"%s"}\n' "$EXPECTED_SHA256" "$POLICY_SHA256" "$SCOPE_SHA256" "$OLD_HELPER_SHA256" "$OLD_POLICY_SHA256" "$OLD_SCOPE_SHA256" "$OWNER_USER" "$OWNER_UID" "$phase" "$PYTHON" "$TEMPLATE_SHA256" >"$MANIFEST_STAGE" || fail
    /usr/bin/chmod 0400 "$MANIFEST_STAGE" || fail
    sync_file "$MANIFEST_STAGE"
    /usr/bin/mv -f -- "$MANIFEST_STAGE" "$MANIFEST" || fail
    sync_file "$HELPER_DIR"
    PHASE=$phase
}
read_manifest() {
    validate_file "$MANIFEST" 400
    fields=$($PYTHON -I -B -S -c 'import json,sys; r=open(sys.argv[1],"rb").read(); v=json.loads(r); assert r==(json.dumps(v,ensure_ascii=True,separators=(",",":"),sort_keys=True)+"\n").encode("ascii"); assert set(v)=={"expected_helper_sha256","expected_policy_sha256","expected_scope_sha256","old_helper_sha256","old_policy_sha256","old_scope_sha256","owner","owner_uid","phase","python","schema","template_sha256"}; print(v["phase"],v["old_helper_sha256"],v["old_policy_sha256"],v["old_scope_sha256"],v["expected_helper_sha256"],v["expected_policy_sha256"],v["expected_scope_sha256"],v["owner"],v["owner_uid"],v["python"],v["schema"],v["template_sha256"])' "$MANIFEST") || fail
    set -- $fields
    [ "$#" -eq 12 ] || fail
    PHASE=$1 OLD_HELPER_SHA256=$2 OLD_POLICY_SHA256=$3 OLD_SCOPE_SHA256=$4
    [ "$5" = "$EXPECTED_SHA256" ] && [ "$6" = "$POLICY_SHA256" ] && [ "$7" = "$SCOPE_SHA256" ] || fail
    [ "$8" = "$OWNER_USER" ] && [ "$9" = "$OWNER_UID" ] && [ "${10}" = "$PYTHON" ] || fail
    [ "${11}" = friday.retention-probe-install.v1 ] && [ "${12}" = "$TEMPLATE_SHA256" ] || fail
    case "$PHASE" in prepared|revoked|helper_installed|policy_published) ;; *) fail ;; esac
    for item in "$OLD_HELPER_SHA256" "$OLD_POLICY_SHA256" "$OLD_SCOPE_SHA256"; do
        if [ "$item" != none ]; then
            [ "${#item}" -eq 64 ] || fail
            case "$item" in *[!0-9a-f]*) fail ;; esac
        fi
    done
}
exact_or_absent() (
    path=$1 mode=$2 expected=$3
    if [ "$expected" = none ]; then
        [ ! -e "$path" ] && [ ! -L "$path" ]
    else
        validate_file "$path" "$mode" || return 1
        observed=$(digest "$path") || return 1
        [ "$observed" = "$expected" ] || return 1
    fi
)
prepare_helper_stage() {
    if [ -e "$HELPER_STAGE" ] || [ -L "$HELPER_STAGE" ]; then
        validate_file "$HELPER_STAGE" 755
        [ "$(digest "$HELPER_STAGE")" = "$EXPECTED_SHA256" ] || fail
    else
        /usr/bin/install -o "$EXPECTED_UID" -g "$EXPECTED_GID" -m 0755 "$SOURCE" "$HELPER_STAGE" || fail
        [ "$(digest "$SOURCE")" = "$EXPECTED_SHA256" ] || fail
        [ "$(digest "$HELPER_STAGE")" = "$EXPECTED_SHA256" ] || fail
        sync_file "$HELPER_STAGE"; sync_file "$HELPER_DIR"
    fi
}
prepare_policy_stage() {
    if [ -e "$SUDOERS_STAGE" ] || [ -L "$SUDOERS_STAGE" ]; then
        validate_file "$SUDOERS_STAGE" 440
        [ "$(digest "$SUDOERS_STAGE")" = "$POLICY_SHA256" ] || fail
    else
        /usr/bin/printf '#%s ALL=(root) NOPASSWD: %s -I -B -S /usr/libexec/friday/release_artifact_proc_probe.py privileged-target-probe\n' "$OWNER_UID" "$PYTHON" >"$SUDOERS_STAGE" || fail
        /usr/bin/chmod 0440 "$SUDOERS_STAGE" || fail
        [ "$(digest "$TEMPLATE")" = "$TEMPLATE_SHA256" ] || fail
        [ "$(digest "$SUDOERS_STAGE")" = "$POLICY_SHA256" ] || fail
        /usr/sbin/visudo -cf "$SUDOERS_STAGE" >/dev/null || fail
        sync_file "$SUDOERS_STAGE"; sync_file "$SUDOERS_DIR"
    fi
}
prepare_scope_stage() {
    if [ -e "$SCOPE_STAGE" ] || [ -L "$SCOPE_STAGE" ]; then
        validate_file "$SCOPE_STAGE" 400
        [ "$(digest "$SCOPE_STAGE")" = "$SCOPE_SHA256" ] || fail
    else
        /usr/bin/printf '%s\n' "$SCOPE_BODY" >"$SCOPE_STAGE" || fail
        /usr/bin/chmod 0400 "$SCOPE_STAGE" || fail
        [ "$(digest "$SCOPE_STAGE")" = "$SCOPE_SHA256" ] || fail
        sync_file "$SCOPE_STAGE"; sync_file "$HELPER_DIR"
    fi
}

if [ -e "$MANIFEST" ] || [ -L "$MANIFEST" ]; then
    read_manifest
else
    for stale in "$MANIFEST_STAGE" "$HELPER_STAGE" "$HELPER_REVOKED" "$SCOPE_STAGE" "$SCOPE_REVOKED" "$SUDOERS_STAGE" "$SUDOERS_REVOKED" "$SUDOERS_REMOVING"; do
        [ ! -e "$stale" ] && [ ! -L "$stale" ] || fail
    done
    if [ -e "$HELPER" ] || [ -L "$HELPER" ]; then validate_file "$HELPER" 755; OLD_HELPER_SHA256=$(digest "$HELPER"); fi
    if [ -e "$SCOPE" ] || [ -L "$SCOPE" ]; then validate_file "$SCOPE" 400; OLD_SCOPE_SHA256=$(digest "$SCOPE"); fi
    if [ -e "$SUDOERS" ] || [ -L "$SUDOERS" ]; then
        validate_file "$SUDOERS" 440
        [ "$OLD_HELPER_SHA256" != none ] || fail
        [ "$OLD_SCOPE_SHA256" != none ] || fail
        /usr/sbin/visudo -cf "$SUDOERS" >/dev/null || fail
        OLD_POLICY_SHA256=$(digest "$SUDOERS")
    fi
    write_manifest prepared
fi
prepare_helper_stage
prepare_scope_stage
prepare_policy_stage

if [ "$PHASE" = prepared ]; then
    if exact_or_absent "$SUDOERS" 440 "$OLD_POLICY_SHA256"; then
        if [ "$OLD_POLICY_SHA256" != none ]; then
            [ ! -e "$SUDOERS_REVOKED" ] && [ ! -L "$SUDOERS_REVOKED" ] || fail
            /usr/bin/mv -- "$SUDOERS" "$SUDOERS_REVOKED" || fail
            sync_file "$SUDOERS_DIR"
        fi
    else
        exact_or_absent "$SUDOERS" 440 none || fail
        exact_or_absent "$SUDOERS_REVOKED" 440 "$OLD_POLICY_SHA256" || fail
    fi
    write_manifest revoked
fi
if [ "$PHASE" = revoked ]; then
    exact_or_absent "$SUDOERS" 440 none || fail
    exact_or_absent "$SUDOERS_REVOKED" 440 "$OLD_POLICY_SHA256" || fail
    if exact_or_absent "$SCOPE" 400 "$OLD_SCOPE_SHA256"; then
        if [ "$OLD_SCOPE_SHA256" != none ]; then
            [ ! -e "$SCOPE_REVOKED" ] && [ ! -L "$SCOPE_REVOKED" ] || fail
            /usr/bin/mv -- "$SCOPE" "$SCOPE_REVOKED" || fail
            sync_file "$HELPER_DIR"
        fi
        /usr/bin/mv -f -- "$SCOPE_STAGE" "$SCOPE" || fail
        sync_file "$HELPER_DIR"
    elif exact_or_absent "$SCOPE" 400 none && exact_or_absent "$SCOPE_REVOKED" 400 "$OLD_SCOPE_SHA256"; then
        /usr/bin/mv -f -- "$SCOPE_STAGE" "$SCOPE" || fail
        sync_file "$HELPER_DIR"
    else
        exact_or_absent "$SCOPE_REVOKED" 400 "$OLD_SCOPE_SHA256" || fail
        validate_file "$SCOPE" 400
        [ "$(digest "$SCOPE")" = "$SCOPE_SHA256" ] || fail
    fi
    if exact_or_absent "$HELPER" 755 "$OLD_HELPER_SHA256"; then
        if [ "$OLD_HELPER_SHA256" != none ]; then
            [ ! -e "$HELPER_REVOKED" ] && [ ! -L "$HELPER_REVOKED" ] || fail
            /usr/bin/mv -- "$HELPER" "$HELPER_REVOKED" || fail
            sync_file "$HELPER_DIR"
        fi
        /usr/bin/mv -f -- "$HELPER_STAGE" "$HELPER" || fail
        sync_file "$HELPER_DIR"
    elif exact_or_absent "$HELPER" 755 none && exact_or_absent "$HELPER_REVOKED" 755 "$OLD_HELPER_SHA256"; then
        /usr/bin/mv -f -- "$HELPER_STAGE" "$HELPER" || fail
        sync_file "$HELPER_DIR"
    else
        exact_or_absent "$HELPER_REVOKED" 755 "$OLD_HELPER_SHA256" || fail
        validate_file "$HELPER" 755
        [ "$(digest "$HELPER")" = "$EXPECTED_SHA256" ] || fail
    fi
    validate_file "$HELPER" 755
    [ "$(digest "$HELPER")" = "$EXPECTED_SHA256" ] || fail
    validate_file "$SCOPE" 400
    [ "$(digest "$SCOPE")" = "$SCOPE_SHA256" ] || fail
    write_manifest helper_installed
    if [ "$TEST_MODE" = 1 ] && [ "${FRIDAY_RETENTION_INSTALL_FAIL_AFTER_HELPER:-0}" = 1 ]; then fail; fi
fi
if [ "$PHASE" = helper_installed ]; then
    validate_file "$HELPER" 755
    [ "$(digest "$HELPER")" = "$EXPECTED_SHA256" ] || fail
    validate_file "$SCOPE" 400
    [ "$(digest "$SCOPE")" = "$SCOPE_SHA256" ] || fail
    exact_or_absent "$SCOPE_REVOKED" 400 "$OLD_SCOPE_SHA256" || fail
    exact_or_absent "$HELPER_REVOKED" 755 "$OLD_HELPER_SHA256" || fail
    if [ -e "$SUDOERS" ] || [ -L "$SUDOERS" ]; then
        validate_file "$SUDOERS" 440
        [ "$(digest "$SUDOERS")" = "$POLICY_SHA256" ] || fail
    else
        /usr/bin/mv -f -- "$SUDOERS_STAGE" "$SUDOERS" || fail
        sync_file "$SUDOERS_DIR"
    fi
    validate_file "$SUDOERS" 440
    [ "$(digest "$SUDOERS")" = "$POLICY_SHA256" ] || fail
    /usr/sbin/visudo -cf "$SUDOERS" >/dev/null || fail
    write_manifest policy_published
fi
[ "$PHASE" = policy_published ] || fail
validate_file "$HELPER" 755; validate_file "$SCOPE" 400; validate_file "$SUDOERS" 440
[ "$(digest "$HELPER")" = "$EXPECTED_SHA256" ] || fail
[ "$(digest "$SCOPE")" = "$SCOPE_SHA256" ] || fail
[ "$(digest "$SUDOERS")" = "$POLICY_SHA256" ] || fail
/usr/sbin/visudo -cf "$SUDOERS" >/dev/null || fail
/usr/bin/rm -f -- "$HELPER_STAGE" "$HELPER_REVOKED" "$SCOPE_STAGE" "$SCOPE_REVOKED" "$SUDOERS_STAGE" "$SUDOERS_REVOKED" || fail
sync_file "$HELPER_DIR"; sync_file "$SUDOERS_DIR"
/usr/bin/rm -f -- "$MANIFEST_STAGE" "$MANIFEST" || fail
sync_file "$HELPER_DIR"
printf '%s\n' friday_retention_probe_installed
