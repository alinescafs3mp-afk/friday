#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONHOME PYTHONPATH
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
umask 077

CONTROL_DIR=/etc/friday-host-control
INSTALL_DIR=/opt/friday-host-control
RELEASES_DIR=$INSTALL_DIR/releases
CURRENT_LINK=$INSTALL_DIR/current
LEGACY_VENV_DIR=$INSTALL_DIR/venv
TRANSACTION_DIR=$INSTALL_DIR/.install-transaction
BROKER_STATE_DIR=/var/lib/friday-package-broker
APPROVAL_SIGNER_GROUP=friday-host-approval
USER_UNIT_DIR=/etc/systemd/user
SYSTEM_UNIT_DIR=/etc/systemd/system
TMPFILES_CONFIG=/etc/tmpfiles.d/friday-host-agent.conf
SOCKET_BASE_DIR=/run/friday-host-agent
ENABLE_SERVICES=0
RECOVER_TRANSACTION=0
TARGET_USER=
FRIDAY_DATA_DIR=
ARTIFACT_WHEEL=
ARTIFACT_SHA256=
TEMP_PATHS=
TEMP_DIRS=
NEW_RELEASE_DIR=
ROLLBACK_DIR=
ACTIVATED=0
COMMITTED=0
CURRENT_WAS_PRESENT=0
CURRENT_PREVIOUS_TARGET=
BROKER_ENABLE_ATTEMPTED=0
USER_ENABLE_ATTEMPTED=0
LINGER_ATTEMPTED=0
USER_MANAGER_STARTED_BY_RUN=0
BROKER_WAS_ENABLED=0
USER_AGENT_WAS_ENABLED=0
BROKER_ENABLE_STATE=
USER_AGENT_ENABLE_STATE=
LINGER_WAS_ENABLED=0
USER_MANAGER_WAS_ACTIVE=0

fail() {
    printf '%s\n' "host-control install: $*" >&2
    exit 1
}

usage() {
    printf '%s\n' \
        "Usage: install.sh --user USER --friday-data-dir ABSOLUTE_DIR \\" \
        "  --artifact-wheel ABSOLUTE_FILE --artifact-sha256 HEX [--enable]" \
        "       install.sh --recover --user USER" \
        "" \
        "The script must be run as root from a trusted Friday release bundle." \
        "--enable also enables the broker socket, user service, and user linger."
}

cleanup_paths() {
    cleanup_failed=0
    for path in $TEMP_PATHS; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            /usr/bin/rm -f -- "$path" || cleanup_failed=1
        fi
    done
    for path in $TEMP_DIRS; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            /usr/bin/rm -rf -- "$path" || cleanup_failed=1
        fi
    done
    [ "$cleanup_failed" -eq 0 ]
}

restore_file() {
    target=$1
    tag=$2
    marker="$ROLLBACK_DIR/$tag.state"
    backup="$ROLLBACK_DIR/$tag.file"
    parent=$(/usr/bin/dirname -- "$target")
    if [ "$(/usr/bin/cat -- "$marker")" = present ]; then
        temporary=$(/usr/bin/mktemp "$parent/.friday-rollback.XXXXXX") || return 1
        if ! /usr/bin/cp --preserve=all -- "$backup" "$temporary"; then
            /usr/bin/rm -f -- "$temporary" >/dev/null 2>&1 || /usr/bin/true
            return 1
        fi
        if ! /usr/bin/mv -fT -- "$temporary" "$target"; then
            /usr/bin/rm -f -- "$temporary" >/dev/null 2>&1 || /usr/bin/true
            return 1
        fi
    else
        /usr/bin/rm -f -- "$target" || return 1
    fi
}

restore_enablement() {
    enablement_failed=0
    if [ "$BROKER_ENABLE_ATTEMPTED" -eq 1 ]; then
        if [ "$BROKER_WAS_ENABLED" -eq 1 ]; then
            /usr/bin/systemctl enable friday-package-broker.socket >/dev/null 2>&1 \
                || enablement_failed=1
        else
            /usr/bin/systemctl disable friday-package-broker.socket >/dev/null 2>&1 \
                || enablement_failed=1
        fi
    fi
    if [ "$USER_ENABLE_ATTEMPTED" -eq 1 ]; then
        if [ "$USER_AGENT_WAS_ENABLED" -eq 1 ]; then
            as_user_systemctl enable friday-host-agent.service >/dev/null 2>&1 \
                || enablement_failed=1
        else
            as_user_systemctl disable friday-host-agent.service >/dev/null 2>&1 \
                || enablement_failed=1
        fi
    fi
    if [ "$LINGER_ATTEMPTED" -eq 1 ]; then
        if [ "$LINGER_WAS_ENABLED" -eq 1 ]; then
            /usr/bin/loginctl enable-linger "$TARGET_USER" >/dev/null 2>&1 \
                || enablement_failed=1
        else
            /usr/bin/loginctl disable-linger "$TARGET_USER" >/dev/null 2>&1 \
                || enablement_failed=1
        fi
    fi
    [ "$enablement_failed" -eq 0 ]
}

rollback_transaction() {
    rollback_failed=0
    if [ "$USER_ENABLE_ATTEMPTED" -eq 1 ]; then
        as_user_systemctl stop friday-host-agent.service >/dev/null 2>&1 || rollback_failed=1
    fi
    if [ "$BROKER_ENABLE_ATTEMPTED" -eq 1 ]; then
        /usr/bin/systemctl stop friday-package-broker.socket friday-package-broker.service \
            >/dev/null 2>&1 || rollback_failed=1
    fi
    # Disable any links created from a previously not-found unit while the
    # candidate unit files still exist and systemctl can resolve their [Install]
    # sections. Restore linger in the same best-effort pass.
    restore_enablement || rollback_failed=1

    if [ "$CURRENT_WAS_PRESENT" -eq 1 ]; then
        rollback_link="$INSTALL_DIR/.current.rollback.$$"
        /usr/bin/rm -f -- "$rollback_link"
        /usr/bin/ln -s -- "$CURRENT_PREVIOUS_TARGET" "$rollback_link" \
            || rollback_failed=1
        /usr/bin/mv -fT -- "$rollback_link" "$CURRENT_LINK" || rollback_failed=1
        /usr/bin/rm -f -- "$rollback_link" >/dev/null 2>&1 || rollback_failed=1
    else
        /usr/bin/rm -f -- "$CURRENT_LINK" || rollback_failed=1
    fi

    restore_file "$CONTROL_DIR/host-agent.env" host_agent_env || rollback_failed=1
    restore_file "$CONTROL_DIR/release.env" release_env || rollback_failed=1
    restore_file "$TMPFILES_CONFIG" tmpfiles || rollback_failed=1
    restore_file "$USER_UNIT_DIR/friday-host-agent.service" host_agent_unit \
        || rollback_failed=1
    restore_file "$SYSTEM_UNIT_DIR/friday-package-broker.service" broker_service \
        || rollback_failed=1
    restore_file "$SYSTEM_UNIT_DIR/friday-package-broker.socket" broker_socket \
        || rollback_failed=1
    restore_file "$USER_UNIT_DIR/friday-host-agent.service.d/20-deployment.conf" \
        host_agent_dropin || rollback_failed=1
    restore_file "$SYSTEM_UNIT_DIR/friday-package-broker.socket.d/20-deployment.conf" \
        broker_socket_dropin || rollback_failed=1
    restore_file /usr/local/share/doc/friday-host-control/README.md readme \
        || rollback_failed=1

    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    if [ -S "/run/user/$USER_UID/bus" ]; then
        as_user_systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    fi
    if [ "$USER_MANAGER_STARTED_BY_RUN" -eq 1 ] && [ "$USER_MANAGER_WAS_ACTIVE" -eq 0 ]; then
        /usr/bin/systemctl stop "user@$USER_UID.service" >/dev/null 2>&1 \
            || rollback_failed=1
    fi
    [ "$rollback_failed" -eq 0 ]
}

current_resolves_to_candidate() {
    [ -n "${CANDIDATE_VENV:-}" ] && [ -L "$CURRENT_LINK" ] \
        && [ "$(/usr/bin/find -P "$CURRENT_LINK" -maxdepth 0 -type l -user root -print)" \
             = "$CURRENT_LINK" ] \
        && [ "$(/usr/bin/readlink -f -- "$CURRENT_LINK" 2>/dev/null)" = "$CANDIDATE_VENV" ]
}

on_exit() {
    status=$?
    trap '' HUP INT TERM
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$ACTIVATED" -eq 1 ] && [ "$COMMITTED" -eq 0 ]; then
        if ! rollback_transaction; then
            printf '%s\n' \
                "host-control install: rollback failed after exit $status; inspect $ROLLBACK_DIR" >&2
            status=125
            # A failed stop/rollback can leave a process executing the candidate
            # even after current was restored. Preserve both evidence and code
            # until an explicit recovery proves that removal is safe.
            TEMP_DIRS=
            NEW_RELEASE_DIR=
        fi
    fi
    if [ "$COMMITTED" -eq 0 ] && [ -n "$NEW_RELEASE_DIR" ] \
        && [ -d "$NEW_RELEASE_DIR" ]; then
        if ! /usr/bin/rm -rf -- "$NEW_RELEASE_DIR"; then
            printf '%s\n' "host-control install: candidate cleanup failed: $NEW_RELEASE_DIR" >&2
            [ "$status" -ne 0 ] || status=125
        fi
    fi
    if ! cleanup_paths; then
        printf '%s\n' "host-control install: temporary cleanup was incomplete" >&2
        [ "$status" -ne 0 ] || status=125
    fi
    exit "$status"
}

trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user)
            [ "$#" -ge 2 ] || fail "--user requires a value"
            TARGET_USER=$2
            shift 2
            ;;
        --friday-data-dir)
            [ "$#" -ge 2 ] || fail "--friday-data-dir requires a value"
            FRIDAY_DATA_DIR=$2
            shift 2
            ;;
        --artifact-wheel)
            [ "$#" -ge 2 ] || fail "--artifact-wheel requires a value"
            ARTIFACT_WHEEL=$2
            shift 2
            ;;
        --artifact-sha256)
            [ "$#" -ge 2 ] || fail "--artifact-sha256 requires a value"
            ARTIFACT_SHA256=$2
            shift 2
            ;;
        --enable)
            ENABLE_SERVICES=1
            shift
            ;;
        --recover)
            RECOVER_TRANSACTION=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[ "$(/usr/bin/id -u)" -eq 0 ] || fail "run this explicit operator setup as root"
[ -n "$TARGET_USER" ] || fail "--user is required"
[ -x /usr/bin/flock ] || fail "Ubuntu util-linux flock is required"
exec 9>/run/lock/friday-host-control.install.lock
/usr/bin/flock -n 9 || fail "another host-control install transaction is active"
if [ "$RECOVER_TRANSACTION" -eq 1 ]; then
    [ -z "$FRIDAY_DATA_DIR" ] && [ -z "$ARTIFACT_WHEEL" ] && [ -z "$ARTIFACT_SHA256" ] \
        && [ "$ENABLE_SERVICES" -eq 0 ] \
        || fail "--recover accepts only --user"
else
    [ -n "$FRIDAY_DATA_DIR" ] || fail "--friday-data-dir is required"
    [ -n "$ARTIFACT_WHEEL" ] || fail "--artifact-wheel is required"
    [ -n "$ARTIFACT_SHA256" ] || fail "--artifact-sha256 is required"
    case "$ARTIFACT_SHA256" in
        *[!0-9a-f]*|'') fail "artifact SHA-256 must be lowercase hexadecimal" ;;
    esac
    [ "${#ARTIFACT_SHA256}" -eq 64 ] || fail "artifact SHA-256 must contain 64 characters"
    case "$ARTIFACT_WHEEL" in
        /*) ;;
        *) fail "Friday wheel path must be absolute" ;;
    esac
    [ -f "$ARTIFACT_WHEEL" ] && [ ! -L "$ARTIFACT_WHEEL" ] \
        || fail "Friday wheel must be one regular non-symlink file"
    [ "$(/usr/bin/readlink -f -- "$ARTIFACT_WHEEL")" = "$ARTIFACT_WHEEL" ] \
        || fail "Friday wheel path must be canonical and contain no symlink component"
fi

case "$TARGET_USER" in
    *[!a-z0-9_-]*|'') fail "user name contains unsupported characters" ;;
esac
USER_UID=$(/usr/bin/id -u -- "$TARGET_USER" 2>/dev/null) || fail "selected user does not exist"
[ "$USER_UID" -ne 0 ] || fail "the host agent must never run as root"
USER_GID=$(/usr/bin/id -g -- "$TARGET_USER")
[ "$USER_UID" -ge 1000 ] && [ "$USER_GID" -ge 1000 ] \
    || fail "selected user UID/GID must be at least 1000 for the container mapping"
[ "$USER_UID" -le 2147483647 ] && [ "$USER_GID" -le 2147483647 ] \
    || fail "selected user UID/GID is out of range"
USER_GROUP=$(/usr/bin/id -gn -- "$TARGET_USER")
SOCKET_DIR=$SOCKET_BASE_DIR/$USER_UID
case "$USER_GROUP" in
    *[!a-z0-9_-]*|'') fail "primary group name contains unsupported characters" ;;
esac
USER_HOME=$(/usr/bin/getent passwd "$TARGET_USER" | /usr/bin/cut -d: -f6)
[ -n "$USER_HOME" ] && [ -d "$USER_HOME" ] && [ ! -L "$USER_HOME" ] \
    || fail "selected user has no canonical home directory"
[ "$(/usr/bin/readlink -f -- "$USER_HOME")" = "$USER_HOME" ] \
    || fail "selected user's home directory contains a symlink"
[ "$(/usr/bin/stat -c %u -- "$USER_HOME")" -eq "$USER_UID" ] \
    || fail "selected user's home directory has the wrong owner"

as_target_user() {
    /usr/sbin/runuser -u "$TARGET_USER" -- /usr/bin/env -i \
        HOME="$USER_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
        PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
        "$@"
}

as_user_systemctl() {
    as_target_user /usr/bin/systemctl --user "$@"
}

journal_read() {
    journal_name=$1
    case "$journal_name" in
        phase|target_user|user_uid|user_gid|user_group|user_home|candidate_venv|\
        new_release_dir|current_was_present|\
        current_previous_target|broker_enable_state|user_agent_enable_state|\
        broker_was_enabled|user_agent_was_enabled|linger_was_enabled|\
        user_manager_was_active|broker_enable_attempted|user_enable_attempted|\
        linger_attempted|user_manager_started_by_run) ;;
        *) fail "rollback journal requested an unknown field" ;;
    esac
    journal_path="$ROLLBACK_DIR/meta.$journal_name"
    [ -f "$journal_path" ] && [ ! -L "$journal_path" ] \
        && [ "$(/usr/bin/stat -c %u:%a:%h -- "$journal_path")" = "0:600:1" ] \
        && [ "$(/usr/bin/stat -c %s -- "$journal_path")" -ge 1 ] \
        && [ "$(/usr/bin/stat -c %s -- "$journal_path")" -le 4096 ] \
        && [ "$(/usr/bin/wc -l <"$journal_path")" -eq 1 ] \
        || fail "rollback journal field is unsafe: $journal_name"
    JOURNAL_VALUE=$(/usr/bin/cat -- "$journal_path")
}

validate_boolean() {
    case "$1" in
        0|1) ;;
        *) fail "rollback journal contains an invalid boolean" ;;
    esac
}

validate_snapshot() {
    snapshot_tag=$1
    snapshot_marker="$ROLLBACK_DIR/$snapshot_tag.state"
    snapshot_backup="$ROLLBACK_DIR/$snapshot_tag.file"
    [ -f "$snapshot_marker" ] && [ ! -L "$snapshot_marker" ] \
        && [ "$(/usr/bin/stat -c %u:%a:%h -- "$snapshot_marker")" = "0:600:1" ] \
        || fail "rollback snapshot marker is unsafe: $snapshot_tag"
    snapshot_state=$(/usr/bin/cat -- "$snapshot_marker")
    case "$snapshot_state" in
        present)
            [ "$(/usr/bin/stat -c %s -- "$snapshot_marker")" -eq 8 ] \
                || fail "rollback snapshot marker is malformed: $snapshot_tag"
            [ -f "$snapshot_backup" ] && [ ! -L "$snapshot_backup" ] \
                && [ "$(/usr/bin/find -P "$snapshot_backup" -maxdepth 0 -type f \
                         -user root -links 1 ! -perm /022 -print)" = "$snapshot_backup" ] \
                && [ "$(/usr/bin/stat -c %s -- "$snapshot_backup")" -le 4194304 ] \
                || fail "rollback snapshot is unsafe: $snapshot_tag"
            ;;
        absent)
            [ "$(/usr/bin/stat -c %s -- "$snapshot_marker")" -eq 7 ] \
                || fail "rollback snapshot marker is malformed: $snapshot_tag"
            [ ! -e "$snapshot_backup" ] && [ ! -L "$snapshot_backup" ] \
                || fail "absent rollback snapshot unexpectedly has data: $snapshot_tag"
            ;;
        *) fail "rollback snapshot marker is invalid: $snapshot_tag" ;;
    esac
}

recover_stale_transaction() {
    ROLLBACK_DIR=$TRANSACTION_DIR
    [ -d "$ROLLBACK_DIR" ] && [ ! -L "$ROLLBACK_DIR" ] \
        && [ "$(/usr/bin/readlink -f -- "$ROLLBACK_DIR")" = "$ROLLBACK_DIR" ] \
        && [ "$(/usr/bin/stat -c %u:%a -- "$ROLLBACK_DIR")" = "0:700" ] \
        || fail "stale rollback journal root is unsafe"

    for journal_temp in "$ROLLBACK_DIR"/.journal.*; do
        if [ -e "$journal_temp" ] || [ -L "$journal_temp" ]; then
            [ -f "$journal_temp" ] && [ ! -L "$journal_temp" ] \
                && [ "$(/usr/bin/stat -c %u:%h -- "$journal_temp")" = "0:1" ] \
                || fail "stale rollback journal contains an unsafe temporary"
            /usr/bin/rm -f -- "$journal_temp" \
                || fail "could not remove stale rollback journal temporary"
        fi
    done
    for hidden_entry in "$ROLLBACK_DIR"/.[!.]* "$ROLLBACK_DIR"/..?*; do
        if [ -e "$hidden_entry" ] || [ -L "$hidden_entry" ]; then
            fail "stale rollback journal contains an unexpected hidden entry"
        fi
    done
    for journal_entry in "$ROLLBACK_DIR"/*; do
        journal_basename=$(/usr/bin/basename -- "$journal_entry")
        case "$journal_basename" in
            meta.phase|meta.target_user|meta.user_uid|meta.user_gid|meta.user_group|\
            meta.user_home|meta.candidate_venv|\
            meta.new_release_dir|meta.current_was_present|meta.current_previous_target|\
            meta.broker_enable_state|meta.user_agent_enable_state|\
            meta.broker_was_enabled|meta.user_agent_was_enabled|meta.linger_was_enabled|\
            meta.user_manager_was_active|meta.broker_enable_attempted|\
            meta.user_enable_attempted|meta.linger_attempted|\
            meta.user_manager_started_by_run|host_agent_env.state|host_agent_env.file|\
            release_env.state|release_env.file|tmpfiles.state|tmpfiles.file|\
            host_agent_unit.state|host_agent_unit.file|broker_service.state|\
            broker_service.file|broker_socket.state|broker_socket.file|\
            host_agent_dropin.state|host_agent_dropin.file|broker_socket_dropin.state|\
            broker_socket_dropin.file|readme.state|readme.file) ;;
            *) fail "stale rollback journal contains an unexpected entry" ;;
        esac
    done
    for snapshot_tag in host_agent_env release_env tmpfiles host_agent_unit \
        broker_service broker_socket host_agent_dropin broker_socket_dropin readme; do
        validate_snapshot "$snapshot_tag"
    done

    journal_read target_user
    [ "$JOURNAL_VALUE" = "$TARGET_USER" ] \
        || fail "--user does not match the stale rollback journal"
    journal_read user_uid
    case "$JOURNAL_VALUE" in
        *[!0-9]*|'') fail "rollback journal user UID is invalid" ;;
    esac
    [ "$JOURNAL_VALUE" -eq "$USER_UID" ] \
        || fail "selected user's UID changed since the failed transaction"
    journal_read user_gid
    case "$JOURNAL_VALUE" in
        *[!0-9]*|'') fail "rollback journal user GID is invalid" ;;
    esac
    [ "$JOURNAL_VALUE" -eq "$USER_GID" ] \
        || fail "selected user's GID changed since the failed transaction"
    journal_read user_group
    [ "$JOURNAL_VALUE" = "$USER_GROUP" ] \
        || fail "selected user's primary group changed since the failed transaction"
    journal_read user_home
    [ "$JOURNAL_VALUE" = "$USER_HOME" ] \
        || fail "selected user's home changed since the failed transaction"

    journal_read phase
    TRANSACTION_PHASE=$JOURNAL_VALUE
    case "$TRANSACTION_PHASE" in
        prepared|publication_armed|published|enabling|committed) ;;
        *) fail "rollback journal phase is invalid" ;;
    esac
    journal_read candidate_venv
    RECOVERY_CANDIDATE_VENV=$JOURNAL_VALUE
    case "$RECOVERY_CANDIDATE_VENV" in
        "$RELEASES_DIR"/*/venv) ;;
        *) fail "rollback candidate escapes the versioned release root" ;;
    esac
    if [ -e "$RECOVERY_CANDIDATE_VENV" ] || [ -L "$RECOVERY_CANDIDATE_VENV" ]; then
        [ -d "$RECOVERY_CANDIDATE_VENV" ] && [ ! -L "$RECOVERY_CANDIDATE_VENV" ] \
            && [ "$(/usr/bin/readlink -f -- "$RECOVERY_CANDIDATE_VENV")" \
                 = "$RECOVERY_CANDIDATE_VENV" ] \
            && [ "$(/usr/bin/stat -c %u -- "$RECOVERY_CANDIDATE_VENV")" -eq 0 ] \
            || fail "rollback candidate venv is unsafe"
    elif [ -L "$CURRENT_LINK" ] \
        && [ "$(/usr/bin/readlink -- "$CURRENT_LINK")" = "$RECOVERY_CANDIDATE_VENV" ]; then
        fail "active rollback candidate venv is missing"
    fi
    journal_read new_release_dir
    RECOVERY_NEW_RELEASE_DIR=$JOURNAL_VALUE
    [ "$RECOVERY_NEW_RELEASE_DIR/venv" = "$RECOVERY_CANDIDATE_VENV" ] \
        || fail "rollback release and candidate venv disagree"

    journal_read current_was_present
    validate_boolean "$JOURNAL_VALUE"
    CURRENT_WAS_PRESENT=$JOURNAL_VALUE
    journal_read current_previous_target
    CURRENT_PREVIOUS_TARGET=$JOURNAL_VALUE
    if [ "$CURRENT_WAS_PRESENT" -eq 1 ]; then
        case "$CURRENT_PREVIOUS_TARGET" in
            "$RELEASES_DIR"/*/venv) ;;
            *) fail "rollback previous activation escapes the release root" ;;
        esac
        [ -d "$CURRENT_PREVIOUS_TARGET" ] && [ ! -L "$CURRENT_PREVIOUS_TARGET" ] \
            && [ "$(/usr/bin/readlink -f -- "$CURRENT_PREVIOUS_TARGET")" \
                 = "$CURRENT_PREVIOUS_TARGET" ] \
            && [ "$(/usr/bin/stat -c %u -- "$CURRENT_PREVIOUS_TARGET")" -eq 0 ] \
            || fail "rollback previous activation target is unavailable"
    else
        [ -z "$CURRENT_PREVIOUS_TARGET" ] \
            || fail "rollback journal has an unexpected previous activation"
    fi

    journal_read broker_enable_state
    BROKER_ENABLE_STATE=$JOURNAL_VALUE
    journal_read user_agent_enable_state
    USER_AGENT_ENABLE_STATE=$JOURNAL_VALUE
    for journal_boolean in broker_was_enabled user_agent_was_enabled linger_was_enabled \
        user_manager_was_active broker_enable_attempted user_enable_attempted \
        linger_attempted user_manager_started_by_run; do
        journal_read "$journal_boolean"
        validate_boolean "$JOURNAL_VALUE"
        case "$journal_boolean" in
            broker_was_enabled) BROKER_WAS_ENABLED=$JOURNAL_VALUE ;;
            user_agent_was_enabled) USER_AGENT_WAS_ENABLED=$JOURNAL_VALUE ;;
            linger_was_enabled) LINGER_WAS_ENABLED=$JOURNAL_VALUE ;;
            user_manager_was_active) USER_MANAGER_WAS_ACTIVE=$JOURNAL_VALUE ;;
            broker_enable_attempted) BROKER_ENABLE_ATTEMPTED=$JOURNAL_VALUE ;;
            user_enable_attempted) USER_ENABLE_ATTEMPTED=$JOURNAL_VALUE ;;
            linger_attempted) LINGER_ATTEMPTED=$JOURNAL_VALUE ;;
            user_manager_started_by_run) USER_MANAGER_STARTED_BY_RUN=$JOURNAL_VALUE ;;
        esac
    done
    CANDIDATE_VENV=$RECOVERY_CANDIDATE_VENV
    NEW_RELEASE_DIR=$RECOVERY_NEW_RELEASE_DIR
    TEMP_DIRS="$ROLLBACK_DIR"
    if [ "$TRANSACTION_PHASE" = committed ]; then
        if ! current_resolves_to_candidate; then
            TEMP_DIRS=
            NEW_RELEASE_DIR=
            fail "committed Host Control transaction has no exact live activation"
        fi
        COMMITTED=1
        NEW_RELEASE_DIR=
        printf '%s\n' "Recovered cleanup for an already committed Host Control installation."
        return 0
    fi
    ACTIVATED=1
    if rollback_transaction; then
        printf '%s\n' "Recovered the exact interrupted Host Control installation transaction."
        return 0
    fi
    ACTIVATED=0
    TEMP_DIRS=
    NEW_RELEASE_DIR=
    printf '%s\n' \
        "host-control install: stale transaction recovery failed; journal preserved at $ROLLBACK_DIR" \
        >&2
    exit 125
}

if [ -e "$TRANSACTION_DIR" ] || [ -L "$TRANSACTION_DIR" ]; then
    [ "$RECOVER_TRANSACTION" -eq 1 ] \
        || fail "stale Host Control transaction found; run install.sh --recover --user $TARGET_USER"
    recover_stale_transaction
    exit 0
fi
[ "$RECOVER_TRANSACTION" -eq 0 ] || fail "no stale Host Control transaction exists"

if /usr/bin/systemctl is-active --quiet friday-package-broker.socket \
    || /usr/bin/systemctl is-active --quiet friday-package-broker.service; then
    fail "stop the package broker socket/service before install or upgrade"
fi
if [ -S "/run/user/$USER_UID/bus" ] \
    && as_user_systemctl is-active --quiet friday-host-agent.service; then
    fail "stop the host-agent user service before install or upgrade"
fi
BROKER_ENABLE_STATE=$(
    /usr/bin/systemctl is-enabled friday-package-broker.socket 2>/dev/null || /usr/bin/true
)
USER_AGENT_ENABLE_STATE=$(
    as_user_systemctl is-enabled friday-host-agent.service 2>/dev/null || /usr/bin/true
)
case "$BROKER_ENABLE_STATE" in
    enabled) BROKER_WAS_ENABLED=1 ;;
    disabled|not-found|'') ;;
    *)
        [ "$ENABLE_SERVICES" -eq 0 ] \
            || fail "broker socket has unsupported pre-install enablement: $BROKER_ENABLE_STATE"
        ;;
esac
case "$USER_AGENT_ENABLE_STATE" in
    enabled) USER_AGENT_WAS_ENABLED=1 ;;
    disabled|not-found|'') ;;
    *)
        [ "$ENABLE_SERVICES" -eq 0 ] \
            || fail "host-agent has unsupported pre-install enablement: $USER_AGENT_ENABLE_STATE"
        ;;
esac
if [ -e "/var/lib/systemd/linger/$TARGET_USER" ]; then
    LINGER_WAS_ENABLED=1
fi
if /usr/bin/systemctl is-active --quiet "user@$USER_UID.service"; then
    USER_MANAGER_WAS_ACTIVE=1
fi

# The broker key and socket use the desktop user's existing primary group.  A
# shared primary group would leak the broker request-signing key, so fail instead of
# silently broadening access or changing login groups behind the operator's back.
/usr/bin/getent passwd | /usr/bin/awk -F: -v gid="$USER_GID" \
    '$4 == gid { count += 1 } END { exit(count == 1 ? 0 : 1) }' \
    || fail "selected user must have a private primary group"
/usr/bin/getent group "$USER_GROUP" | /usr/bin/awk -F: -v user="$TARGET_USER" \
    '$4 == "" || $4 == user { ok = 1 } END { exit(ok ? 0 : 1) }' \
    || fail "selected user's primary group has additional explicit members"

# The backend signer is deliberately not readable by the selected desktop UID.
# Docker grants this otherwise memberless supplemental GID only to the backend
# container; the same-UID host agent therefore cannot forge an approval proof.
if ! /usr/bin/getent group "$APPROVAL_SIGNER_GROUP" >/dev/null; then
    /usr/sbin/groupadd --system "$APPROVAL_SIGNER_GROUP" \
        || fail "could not create the isolated backend approval signer group"
fi
APPROVAL_SIGNER_GID=$(/usr/bin/getent group "$APPROVAL_SIGNER_GROUP" | /usr/bin/cut -d: -f3)
case "$APPROVAL_SIGNER_GID" in
    *[!0-9]*|'') fail "approval signer group has an invalid GID" ;;
esac
[ "$APPROVAL_SIGNER_GID" -ne "$USER_GID" ] \
    || fail "approval signer group must differ from the desktop user's primary group"
/usr/bin/getent group "$APPROVAL_SIGNER_GROUP" | /usr/bin/awk -F: \
    '$4 == "" { ok = 1 } END { exit(ok ? 0 : 1) }' \
    || fail "approval signer group must have no host user members"
/usr/bin/getent passwd | /usr/bin/awk -F: -v gid="$APPROVAL_SIGNER_GID" \
    '$4 == gid { found = 1 } END { exit(found ? 1 : 0) }' \
    || fail "approval signer GID cannot be any host user's primary group"

case "$FRIDAY_DATA_DIR" in
    /*) ;;
    *) fail "Friday data directory must be absolute" ;;
esac
case "$FRIDAY_DATA_DIR" in
    *[!A-Za-z0-9_./-]*) fail "Friday data directory contains unsupported characters" ;;
esac
case "$FRIDAY_DATA_DIR" in
    /|/home|/etc|/usr|/var|/run|/proc|/sys|/dev)
        fail "Friday data directory is too broad"
        ;;
esac
[ -d "$FRIDAY_DATA_DIR" ] && [ ! -L "$FRIDAY_DATA_DIR" ] \
    || fail "Friday data directory must already exist and cannot be a symlink"
[ "$(/usr/bin/readlink -f -- "$FRIDAY_DATA_DIR")" = "$FRIDAY_DATA_DIR" ] \
    || fail "Friday data directory must be canonical and contain no symlink component"
[ "$(/usr/bin/stat -c %u -- "$FRIDAY_DATA_DIR")" -eq "$USER_UID" ] \
    || fail "Friday data directory must be owned by the selected user"

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
[ -f "$SCRIPT_DIR/verify_wheel.py" ] || fail "trusted wheel verifier was not found"
[ -f "$SCRIPT_DIR/prepare_user_assets.py" ] && [ ! -L "$SCRIPT_DIR/prepare_user_assets.py" ] \
    || fail "trusted user-asset helper was not found"
[ -f "$SCRIPT_DIR/examples/host-agent-policy.toml" ] \
    && [ ! -L "$SCRIPT_DIR/examples/host-agent-policy.toml" ] \
    || fail "trusted host-agent policy example was not found"

OS_ID=$(/usr/bin/sed -n 's/^ID=//p' /etc/os-release | /usr/bin/tr -d '"')
[ "$OS_ID" = ubuntu ] || fail "this installer is intentionally limited to Ubuntu"

prepare_directory() {
    directory=$1
    owner_uid=$2
    owner_name=$3
    group_name=$4
    mode=$5
    if [ -e "$directory" ] || [ -L "$directory" ]; then
        [ -d "$directory" ] && [ ! -L "$directory" ] \
            || fail "refusing unsafe existing directory: $directory"
        [ "$(/usr/bin/readlink -f -- "$directory")" = "$directory" ] \
            || fail "directory contains a symlink component: $directory"
        [ "$(/usr/bin/stat -c %u -- "$directory")" -eq "$owner_uid" ] \
            || fail "directory has the wrong owner: $directory"
    else
        /usr/bin/install -d -o "$owner_name" -g "$group_name" -m "0$mode" "$directory"
    fi
    /usr/bin/chown "$owner_name:$group_name" "$directory"
    /usr/bin/chmod "0$mode" "$directory"
}

prepare_directory "$CONTROL_DIR" 0 root "$USER_GROUP" 750
prepare_directory "$INSTALL_DIR" 0 root root 755
prepare_directory "$RELEASES_DIR" 0 root root 755
prepare_directory "$BROKER_STATE_DIR" 0 root root 700
prepare_directory "$SOCKET_BASE_DIR" 0 root root 711
prepare_directory "$SOCKET_DIR" "$USER_UID" "$TARGET_USER" "$USER_GROUP" 700
# Everything below HOME or the user-owned data root is created and sealed by
# that same unprivileged user through dirfd/O_NOFOLLOW operations.  Root never
# chowns, chmods or creates a pathname which the selected user can swap.
as_target_user /usr/bin/python3 -I "$SCRIPT_DIR/prepare_user_assets.py" \
    --home "$USER_HOME" --data-dir "$FRIDAY_DATA_DIR" \
    || fail "selected-user Host Control assets could not be prepared safely"

ensure_key() {
    destination=$1
    owner_uid=$2
    owner_name=$3
    group_id=$4
    group_name=$5
    mode=$6
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        [ -f "$destination" ] && [ ! -L "$destination" ] \
            || fail "refusing unsafe existing key: $destination"
        [ "$(/usr/bin/stat -c %h -- "$destination")" -eq 1 ] \
            || fail "key has more than one hard link: $destination"
        [ "$(/usr/bin/stat -c %u -- "$destination")" -eq "$owner_uid" ] \
            || fail "key has the wrong owner: $destination"
        [ "$(/usr/bin/stat -c %g -- "$destination")" -eq "$group_id" ] \
            || fail "key has the wrong group: $destination"
        [ "$(/usr/bin/stat -c %a -- "$destination")" = "$mode" ] \
            || fail "key permissions must be $mode: $destination"
        key_size=$(/usr/bin/stat -c %s -- "$destination")
        [ "$key_size" -ge 32 ] && [ "$key_size" -le 64 ] \
            || fail "key must contain 32 to 64 raw bytes: $destination"
        return
    fi
    temporary=$(/usr/bin/mktemp "$CONTROL_DIR/.key.XXXXXX")
    TEMP_PATHS="$TEMP_PATHS $temporary"
    [ -x /usr/bin/openssl ] || fail "Ubuntu openssl is required to generate HMAC keys"
    secret=$(/usr/bin/openssl rand -hex 24)
    [ "${#secret}" -eq 48 ] || fail "openssl returned an invalid HMAC key"
    /usr/bin/printf '%s' "$secret" >"$temporary"
    /usr/bin/install -o "$owner_name" -g "$group_name" -m "0$mode" \
        "$temporary" "$destination"
    /usr/bin/rm -f -- "$temporary"
}

AGENT_KEY="$USER_HOME/.config/friday-host-agent/agent.key"
BROKER_KEY="$CONTROL_DIR/broker.key"
ensure_key "$BROKER_KEY" 0 root "$USER_GID" "$USER_GROUP" 640

wheel_temp=$(/usr/bin/mktemp --suffix=.whl "$INSTALL_DIR/.friday-wheel.XXXXXX")
TEMP_PATHS="$TEMP_PATHS $wheel_temp"
/usr/bin/install -o root -g root -m 0400 "$ARTIFACT_WHEEL" "$wheel_temp"
/usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    /usr/bin/python3 -I "$SCRIPT_DIR/verify_wheel.py" "$wheel_temp" "$ARTIFACT_SHA256" \
    || fail "Friday wheel failed offline artifact verification"

if [ ! -e "$CONTROL_DIR/broker-policy.toml" ]; then
    policy_temp=$(/usr/bin/mktemp "$CONTROL_DIR/.policy.XXXXXX")
    TEMP_PATHS="$TEMP_PATHS $policy_temp"
    /usr/bin/sed \
        "s/allowed_peer_uids = \[1000\]/allowed_peer_uids = [$USER_UID]/" \
        "$SCRIPT_DIR/examples/policy.toml" >"$policy_temp"
    /usr/bin/install -o root -g root -m 0640 "$policy_temp" \
        "$CONTROL_DIR/broker-policy.toml"
    /usr/bin/rm -f -- "$policy_temp"
fi

HOST_AGENT_POLICY="$CONTROL_DIR/host-agent-policy.toml"
if [ ! -e "$HOST_AGENT_POLICY" ] && [ ! -L "$HOST_AGENT_POLICY" ]; then
    /usr/bin/install -o root -g root -m 0644 \
        "$SCRIPT_DIR/examples/host-agent-policy.toml" "$HOST_AGENT_POLICY"
fi

attest_root_venv_tree() {
    venv_root=$1
    [ -d "$venv_root" ] && [ ! -L "$venv_root" ] \
        && [ "$(/usr/bin/readlink -f -- "$venv_root")" = "$venv_root" ] \
        && [ "$(/usr/bin/stat -c %u:%g -- "$venv_root")" = "0:0" ] \
        || fail "existing host-control venv root is unsafe"
    unsafe_entry=$(/usr/bin/find "$venv_root" -xdev \
        \( \( -type d -o -type f \) \( ! -user root -o -perm /022 \) \
        -o \( -type l ! -user root \) \
        -o \( ! -type d ! -type f ! -type l \) \) \
        -print -quit)
    [ -z "$unsafe_entry" ] \
        || fail "existing host-control venv contains an unsafe entry"
}

if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ]; then
    [ -L "$CURRENT_LINK" ] \
        && [ "$(/usr/bin/find -P "$CURRENT_LINK" -maxdepth 0 -type l -user root -print)" \
             = "$CURRENT_LINK" ] \
        || fail "host-control current activation must be one root-owned symlink"
    CURRENT_PREVIOUS_TARGET=$(/usr/bin/readlink -- "$CURRENT_LINK")
    case "$CURRENT_PREVIOUS_TARGET" in
        "$RELEASES_DIR"/*/venv) ;;
        *) fail "host-control current activation escapes the versioned release root" ;;
    esac
    [ -d "$CURRENT_PREVIOUS_TARGET" ] && [ ! -L "$CURRENT_PREVIOUS_TARGET" ] \
        && [ "$(/usr/bin/readlink -f -- "$CURRENT_PREVIOUS_TARGET")" \
             = "$CURRENT_PREVIOUS_TARGET" ] \
        || fail "host-control current activation target is unsafe"
    attest_root_venv_tree "$CURRENT_PREVIOUS_TARGET"
    CURRENT_WAS_PRESENT=1
fi
if [ -e "$LEGACY_VENV_DIR" ] || [ -L "$LEGACY_VENV_DIR" ]; then
    [ ! -L "$LEGACY_VENV_DIR" ] || fail "legacy host-control venv cannot be a symlink"
    attest_root_venv_tree "$LEGACY_VENV_DIR"
fi

NEW_RELEASE_DIR=$(/usr/bin/mktemp -d "$RELEASES_DIR/$ARTIFACT_SHA256.XXXXXX")
/usr/bin/chown root:root "$NEW_RELEASE_DIR"
/usr/bin/chmod 0755 "$NEW_RELEASE_DIR"
CANDIDATE_VENV=$NEW_RELEASE_DIR/venv
/usr/bin/python3 -m venv --system-site-packages "$CANDIDATE_VENV"
/usr/bin/chown root:root "$CANDIDATE_VENV"
/usr/bin/chmod 0755 "$CANDIDATE_VENV"
attest_root_venv_tree "$CANDIDATE_VENV"
/usr/bin/grep -Eq '^include-system-site-packages = true$' "$CANDIDATE_VENV/pyvenv.cfg" \
    || fail "candidate venv does not expose Ubuntu python3-apt"
"$CANDIDATE_VENV/bin/python" -c 'import apt, apt_pkg' \
    || fail "python3-apt is unavailable; install Ubuntu python3-apt before retrying"
"$CANDIDATE_VENV/bin/python" - <<'PY' \
    || fail "Python >=3.11 and cryptography >=41.0.7,<51 with Ed25519 support are required"
import re
import sys
from importlib.metadata import version

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if sys.version_info < (3, 11):
    raise SystemExit("Friday requires Python >=3.11")
installed = version("cryptography")
match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", installed)
if match is None:
    raise SystemExit("cryptography must have a stable numeric release version")
release = tuple(int(item) for item in match.groups())
if not (release >= (41, 0, 7) and release < (51, 0, 0)):
    raise SystemExit("cryptography version is outside >=41.0.7,<51")

private_key = Ed25519PrivateKey.generate()
private_raw = private_key.private_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
)
public_raw = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
if len(private_raw) != 32 or len(public_raw) != 32:
    raise SystemExit("cryptography returned an invalid raw Ed25519 key")
payload = b"friday-host-control-preflight-v1"
signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(payload)
Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, payload)
PY
"$CANDIDATE_VENV/bin/python" -m pip --isolated install --disable-pip-version-check \
    --no-index --no-deps --force-reinstall "$wheel_temp"
attest_root_venv_tree "$CANDIDATE_VENV"
[ -x "$CANDIDATE_VENV/bin/friday-host-agent" ] \
    || fail "installed Friday build has no friday-host-agent entrypoint"
[ -x "$CANDIDATE_VENV/bin/friday-package-broker" ] \
    || fail "installed Friday build has no friday-package-broker entrypoint"

BUILD_ID=$("$CANDIDATE_VENV/bin/python" - <<'PY'
import hashlib
import re
from importlib.metadata import distribution, version
from pathlib import Path

value = version("friday")
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}", value) is None:
    raise SystemExit("installed Friday package has an invalid release identity")
installed = distribution("friday")
digest = hashlib.sha256()
for package_root in ("friday/host_control", "friday_host_agent", "friday_package_broker"):
    root = Path(installed.locate_file(package_root))
    if not root.is_dir():
        raise SystemExit(f"installed Host Control package is missing: {package_root}")
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(package_root.encode("ascii") + b"/" + relative + b"\0")
        digest.update(path.read_bytes())
print(f"friday-{value}-{digest.hexdigest()[:24]}")
PY
)
ENV_STAGE=$(/usr/bin/mktemp "$CONTROL_DIR/.host-agent.env.candidate.XXXXXX")
TEMP_PATHS="$TEMP_PATHS $ENV_STAGE"
/usr/bin/printf '%s\n' \
    "FRIDAY_HOST_AGENT_ID=local-user-agent" \
    "FRIDAY_HOST_AGENT_ALLOWED_PEER_UID=$USER_UID" \
    "FRIDAY_HOST_AGENT_SOCKET=$SOCKET_DIR/agent.sock" \
    "FRIDAY_HOST_AGENT_MAX_CONCURRENCY=2" \
    "FRIDAY_HOST_JOB_ROOT=$FRIDAY_DATA_DIR/host-control/jobs" >"$ENV_STAGE"
/usr/bin/chown root:"$USER_GROUP" "$ENV_STAGE"
/usr/bin/chmod 0640 "$ENV_STAGE"
RELEASE_ENV_STAGE=$(/usr/bin/mktemp "$CONTROL_DIR/.release.env.candidate.XXXXXX")
TEMP_PATHS="$TEMP_PATHS $RELEASE_ENV_STAGE"
/usr/bin/printf 'FRIDAY_HOST_CONTROL_BUILD_ID=%s\n' "$BUILD_ID" >"$RELEASE_ENV_STAGE"
/usr/bin/chown root:"$USER_GROUP" "$RELEASE_ENV_STAGE"
/usr/bin/chmod 0640 "$RELEASE_ENV_STAGE"

SIGNING_KEY="$CONTROL_DIR/broker-signing.key"
SIGNING_PUBLIC_KEY="$CONTROL_DIR/broker-signing.pub"
if { [ -e "$SIGNING_KEY" ] || [ -L "$SIGNING_KEY" ]; } \
    || { [ -e "$SIGNING_PUBLIC_KEY" ] || [ -L "$SIGNING_PUBLIC_KEY" ]; }; then
    [ -f "$SIGNING_KEY" ] && [ ! -L "$SIGNING_KEY" ] \
        && [ "$(/usr/bin/stat -c %h -- "$SIGNING_KEY")" -eq 1 ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a:%s -- "$SIGNING_KEY")" = "0:0:600:32" ] \
        || fail "existing broker signing key metadata is unsafe"
    [ -f "$SIGNING_PUBLIC_KEY" ] && [ ! -L "$SIGNING_PUBLIC_KEY" ] \
        && [ "$(/usr/bin/stat -c %h -- "$SIGNING_PUBLIC_KEY")" -eq 1 ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a:%s -- "$SIGNING_PUBLIC_KEY")" \
             = "0:$USER_GID:640:32" ] \
        || fail "existing broker public-key metadata is unsafe"
else
    signing_temp=$(/usr/bin/mktemp "$CONTROL_DIR/.signing.XXXXXX")
    public_temp=$(/usr/bin/mktemp "$CONTROL_DIR/.public.XXXXXX")
    TEMP_PATHS="$TEMP_PATHS $signing_temp $public_temp"
    "$CANDIDATE_VENV/bin/python" - "$signing_temp" "$public_temp" <<'PY'
import pathlib
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.generate()
pathlib.Path(sys.argv[1]).write_bytes(
    private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
)
pathlib.Path(sys.argv[2]).write_bytes(
    private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
)
PY
    /usr/bin/install -o root -g root -m 0600 "$signing_temp" "$SIGNING_KEY"
    /usr/bin/install -o root -g "$USER_GROUP" -m 0640 \
        "$public_temp" "$SIGNING_PUBLIC_KEY"
    /usr/bin/rm -f -- "$signing_temp" "$public_temp"
fi
"$CANDIDATE_VENV/bin/python" - "$SIGNING_KEY" "$SIGNING_PUBLIC_KEY" <<'PY'
import hmac
import pathlib
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.from_private_bytes(pathlib.Path(sys.argv[1]).read_bytes())
observed = pathlib.Path(sys.argv[2]).read_bytes()
expected = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
if not hmac.compare_digest(observed, expected):
    raise SystemExit("broker signing public key does not match its private seed")
PY

APPROVAL_SIGNING_KEY="$CONTROL_DIR/backend-approval-signing.key"
APPROVAL_SIGNING_PUBLIC_KEY="$CONTROL_DIR/backend-approval-signing.pub"
if { [ -e "$APPROVAL_SIGNING_KEY" ] || [ -L "$APPROVAL_SIGNING_KEY" ]; } \
    || { [ -e "$APPROVAL_SIGNING_PUBLIC_KEY" ] || [ -L "$APPROVAL_SIGNING_PUBLIC_KEY" ]; }; then
    [ -f "$APPROVAL_SIGNING_KEY" ] && [ ! -L "$APPROVAL_SIGNING_KEY" ] \
        && [ "$(/usr/bin/stat -c %h -- "$APPROVAL_SIGNING_KEY")" -eq 1 ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a:%s -- "$APPROVAL_SIGNING_KEY")" \
             = "0:$APPROVAL_SIGNER_GID:640:32" ] \
        || fail "existing backend approval signing key metadata is unsafe"
    [ -f "$APPROVAL_SIGNING_PUBLIC_KEY" ] && [ ! -L "$APPROVAL_SIGNING_PUBLIC_KEY" ] \
        && [ "$(/usr/bin/stat -c %h -- "$APPROVAL_SIGNING_PUBLIC_KEY")" -eq 1 ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a:%s -- "$APPROVAL_SIGNING_PUBLIC_KEY")" \
             = "0:$USER_GID:640:32" ] \
        || fail "existing backend approval public-key metadata is unsafe"
else
    approval_signing_temp=$(/usr/bin/mktemp "$CONTROL_DIR/.approval-signing.XXXXXX")
    approval_public_temp=$(/usr/bin/mktemp "$CONTROL_DIR/.approval-public.XXXXXX")
    TEMP_PATHS="$TEMP_PATHS $approval_signing_temp $approval_public_temp"
    "$CANDIDATE_VENV/bin/python" - "$approval_signing_temp" "$approval_public_temp" <<'PY'
import pathlib
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.generate()
pathlib.Path(sys.argv[1]).write_bytes(
    private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
)
pathlib.Path(sys.argv[2]).write_bytes(
    private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
)
PY
    /usr/bin/install -o root -g "$APPROVAL_SIGNER_GROUP" -m 0640 \
        "$approval_signing_temp" "$APPROVAL_SIGNING_KEY"
    /usr/bin/install -o root -g "$USER_GROUP" -m 0640 \
        "$approval_public_temp" "$APPROVAL_SIGNING_PUBLIC_KEY"
    /usr/bin/rm -f -- "$approval_signing_temp" "$approval_public_temp"
fi
"$CANDIDATE_VENV/bin/python" - "$APPROVAL_SIGNING_KEY" "$APPROVAL_SIGNING_PUBLIC_KEY" <<'PY'
import hmac
import pathlib
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.from_private_bytes(pathlib.Path(sys.argv[1]).read_bytes())
observed = pathlib.Path(sys.argv[2]).read_bytes()
expected = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
if not hmac.compare_digest(observed, expected):
    raise SystemExit("backend approval public key does not match its private seed")
PY
# Exercise the exact runtime identity: desktop UID + isolated supplemental GID.
/usr/sbin/runuser -u "$TARGET_USER" -g "$USER_GROUP" -G "$APPROVAL_SIGNER_GROUP" -- \
    /usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    "$CANDIDATE_VENV/bin/python" - "$APPROVAL_SIGNING_KEY" <<'PY'
import sys
from friday_package_broker.approval import load_backend_approval_signing_key

if len(load_backend_approval_signing_key(sys.argv[1])) != 32:
    raise SystemExit("backend approval signer key preflight failed")
PY

"$CANDIDATE_VENV/bin/python" - "$CONTROL_DIR/broker-policy.toml" "$USER_UID" <<'PY'
import sys
from friday_package_broker.policy import load_broker_policy

policy = load_broker_policy(sys.argv[1])
if int(sys.argv[2]) not in policy.allowed_peer_uids:
    raise SystemExit("broker policy does not admit the selected host-agent UID")
if policy.allowed_packages != frozenset({"nmap"}):
    raise SystemExit("first-release broker policy must admit exactly nmap")
PY
"$CANDIDATE_VENV/bin/friday-package-broker" \
    --check-config \
    --systemd-socket \
    --socket /run/friday-package-broker/broker.sock \
    --policy "$CONTROL_DIR/broker-policy.toml" \
    --key-file "$BROKER_KEY" \
    --signing-key-file "$SIGNING_KEY" \
    --approval-verification-public-key-file "$APPROVAL_SIGNING_PUBLIC_KEY" \
    --state-dir "$BROKER_STATE_DIR" \
    --build-id "$BUILD_ID"
as_target_user "$CANDIDATE_VENV/bin/friday-host-agent" \
    --check-config \
    --socket "$SOCKET_DIR/agent.sock" \
    --key-file "$AGENT_KEY" \
    --state-dir "$USER_HOME/.local/state/friday-host-agent" \
    --job-root "$FRIDAY_DATA_DIR/host-control/jobs" \
    --network-policy "$HOST_AGENT_POLICY" \
    --network-approval-public-key-file "$APPROVAL_SIGNING_PUBLIC_KEY" \
    --agent-id local-user-agent \
    --allowed-peer-uid "$USER_UID" \
    --max-concurrency 2 \
    --build-id "$BUILD_ID" \
    --broker-socket /run/friday-package-broker/broker.sock \
    --broker-key-file "$BROKER_KEY" \
    --broker-signing-public-key-file "$SIGNING_PUBLIC_KEY" \
    || fail "installed host-agent configuration failed its unprivileged preflight"

/usr/bin/install -d -o root -g root -m 0755 \
    "$USER_UNIT_DIR/friday-host-agent.service.d" \
    "$SYSTEM_UNIT_DIR/friday-package-broker.socket.d" \
    /usr/local/share/doc/friday-host-control

HOST_AGENT_UNIT_STAGE=$(/usr/bin/mktemp "$USER_UNIT_DIR/.friday-host-agent.service.XXXXXX")
BROKER_SERVICE_STAGE=$(/usr/bin/mktemp "$SYSTEM_UNIT_DIR/.friday-package-broker.service.XXXXXX")
BROKER_SOCKET_STAGE=$(/usr/bin/mktemp "$SYSTEM_UNIT_DIR/.friday-package-broker.socket.XXXXXX")
README_STAGE=$(/usr/bin/mktemp "/usr/local/share/doc/friday-host-control/.README.XXXXXX")
TMPFILES_STAGE=$(/usr/bin/mktemp "/etc/tmpfiles.d/.friday-host-agent.conf.XXXXXX")
AGENT_DROPIN_STAGE=$(/usr/bin/mktemp \
    "$USER_UNIT_DIR/friday-host-agent.service.d/.20-deployment.conf.XXXXXX")
SOCKET_DROPIN_STAGE=$(/usr/bin/mktemp \
    "$SYSTEM_UNIT_DIR/friday-package-broker.socket.d/.20-deployment.conf.XXXXXX")
TEMP_PATHS="$TEMP_PATHS $HOST_AGENT_UNIT_STAGE $BROKER_SERVICE_STAGE"
TEMP_PATHS="$TEMP_PATHS $BROKER_SOCKET_STAGE $README_STAGE $TMPFILES_STAGE"
TEMP_PATHS="$TEMP_PATHS $AGENT_DROPIN_STAGE $SOCKET_DROPIN_STAGE"
/usr/bin/install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/user/friday-host-agent.service" "$HOST_AGENT_UNIT_STAGE"
/usr/bin/install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/system/friday-package-broker.service" "$BROKER_SERVICE_STAGE"
/usr/bin/install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/system/friday-package-broker.socket" "$BROKER_SOCKET_STAGE"
/usr/bin/install -o root -g root -m 0644 "$SCRIPT_DIR/README.md" "$README_STAGE"
/usr/bin/sed \
    -e "s/@USER_UID@/$USER_UID/g" \
    -e "s/@USER_GID@/$USER_GID/g" \
    "$SCRIPT_DIR/systemd/tmpfiles/friday-host-agent.conf.in" >"$TMPFILES_STAGE"
/usr/bin/chown root:root "$TMPFILES_STAGE"
/usr/bin/chmod 0644 "$TMPFILES_STAGE"
/usr/bin/printf '[Service]\nReadWritePaths=%s\nReadWritePaths=%s\n' \
    "$SOCKET_DIR" "$FRIDAY_DATA_DIR/host-control/jobs" >"$AGENT_DROPIN_STAGE"
/usr/bin/chown root:root "$AGENT_DROPIN_STAGE"
/usr/bin/chmod 0644 "$AGENT_DROPIN_STAGE"
/usr/bin/printf '[Socket]\nSocketGroup=%s\n' "$USER_GROUP" >"$SOCKET_DROPIN_STAGE"
/usr/bin/chown root:root "$SOCKET_DROPIN_STAGE"
/usr/bin/chmod 0644 "$SOCKET_DROPIN_STAGE"

snapshot_file() {
    target=$1
    tag=$2
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ -f "$target" ] && [ ! -L "$target" ] \
            && [ "$(/usr/bin/find -P "$target" -maxdepth 0 -type f -user root \
                     -links 1 ! -perm /022 -print)" = "$target" ] \
            || fail "refusing unsafe existing transactional file: $target"
        /usr/bin/cp --preserve=all -- \
            "$target" "$ROLLBACK_DIR/$tag.file"
        /usr/bin/printf 'present\n' >"$ROLLBACK_DIR/$tag.state"
    else
        /usr/bin/printf 'absent\n' >"$ROLLBACK_DIR/$tag.state"
    fi
}

activate_file() {
    staged=$1
    target=$2
    /usr/bin/mv -fT -- "$staged" "$target"
}

journal_write() {
    journal_name=$1
    journal_value=$2
    case "$journal_name" in
        phase|target_user|user_uid|user_gid|user_group|user_home|candidate_venv|\
        new_release_dir|current_was_present|\
        current_previous_target|broker_enable_state|user_agent_enable_state|\
        broker_was_enabled|user_agent_was_enabled|linger_was_enabled|\
        user_manager_was_active|broker_enable_attempted|user_enable_attempted|\
        linger_attempted|user_manager_started_by_run) ;;
        *) fail "refusing unknown rollback journal field" ;;
    esac
    journal_temp=$(/usr/bin/mktemp "$ROLLBACK_DIR/.journal.XXXXXX")
    TEMP_PATHS="$TEMP_PATHS $journal_temp"
    /usr/bin/printf '%s\n' "$journal_value" >"$journal_temp"
    /usr/bin/chown root:root "$journal_temp"
    /usr/bin/chmod 0600 "$journal_temp"
    /usr/bin/mv -fT -- "$journal_temp" "$ROLLBACK_DIR/meta.$journal_name"
    /usr/bin/sync -f "$ROLLBACK_DIR/meta.$journal_name"
}

ROLLBACK_CANDIDATE=$(/usr/bin/mktemp -d "$INSTALL_DIR/.install-transaction.candidate.XXXXXX")
ROLLBACK_DIR=$ROLLBACK_CANDIDATE
/usr/bin/chown root:root "$ROLLBACK_DIR"
/usr/bin/chmod 0700 "$ROLLBACK_DIR"
TEMP_DIRS="$TEMP_DIRS $ROLLBACK_DIR"
snapshot_file "$CONTROL_DIR/host-agent.env" host_agent_env
snapshot_file "$CONTROL_DIR/release.env" release_env
snapshot_file "$TMPFILES_CONFIG" tmpfiles
snapshot_file "$USER_UNIT_DIR/friday-host-agent.service" host_agent_unit
snapshot_file "$SYSTEM_UNIT_DIR/friday-package-broker.service" broker_service
snapshot_file "$SYSTEM_UNIT_DIR/friday-package-broker.socket" broker_socket
snapshot_file "$USER_UNIT_DIR/friday-host-agent.service.d/20-deployment.conf" \
    host_agent_dropin
snapshot_file "$SYSTEM_UNIT_DIR/friday-package-broker.socket.d/20-deployment.conf" \
    broker_socket_dropin
snapshot_file /usr/local/share/doc/friday-host-control/README.md readme
journal_write phase prepared
journal_write target_user "$TARGET_USER"
journal_write user_uid "$USER_UID"
journal_write user_gid "$USER_GID"
journal_write user_group "$USER_GROUP"
journal_write user_home "$USER_HOME"
journal_write candidate_venv "$CANDIDATE_VENV"
journal_write new_release_dir "$NEW_RELEASE_DIR"
journal_write current_was_present "$CURRENT_WAS_PRESENT"
journal_write current_previous_target "$CURRENT_PREVIOUS_TARGET"
journal_write broker_enable_state "$BROKER_ENABLE_STATE"
journal_write user_agent_enable_state "$USER_AGENT_ENABLE_STATE"
journal_write broker_was_enabled "$BROKER_WAS_ENABLED"
journal_write user_agent_was_enabled "$USER_AGENT_WAS_ENABLED"
journal_write linger_was_enabled "$LINGER_WAS_ENABLED"
journal_write user_manager_was_active "$USER_MANAGER_WAS_ACTIVE"
journal_write broker_enable_attempted 0
journal_write user_enable_attempted 0
journal_write linger_attempted 0
journal_write user_manager_started_by_run 0
[ ! -e "$TRANSACTION_DIR" ] && [ ! -L "$TRANSACTION_DIR" ] \
    || fail "a Host Control transaction journal appeared concurrently"
/usr/bin/sync -f "$ROLLBACK_DIR"
/usr/bin/mv -T -- "$ROLLBACK_CANDIDATE" "$TRANSACTION_DIR"
ROLLBACK_DIR=$TRANSACTION_DIR
TEMP_DIRS="$TEMP_DIRS $ROLLBACK_DIR"
/usr/bin/sync -f "$INSTALL_DIR"

CURRENT_STAGE="$INSTALL_DIR/.current.candidate.$$"
[ ! -e "$CURRENT_STAGE" ] && [ ! -L "$CURRENT_STAGE" ] \
    || fail "stale current activation candidate exists"
/usr/bin/ln -s -- "$CANDIDATE_VENV" "$CURRENT_STAGE"
TEMP_PATHS="$TEMP_PATHS $CURRENT_STAGE"
# Arm rollback before the publication syscall so a signal cannot land after
# the link changed but before the EXIT trap knows that live state was touched.
journal_write phase publication_armed
ACTIVATED=1
/usr/bin/mv -fT -- "$CURRENT_STAGE" "$CURRENT_LINK"
journal_write phase published

activate_file "$ENV_STAGE" "$CONTROL_DIR/host-agent.env"
activate_file "$RELEASE_ENV_STAGE" "$CONTROL_DIR/release.env"
activate_file "$TMPFILES_STAGE" "$TMPFILES_CONFIG"
activate_file "$HOST_AGENT_UNIT_STAGE" "$USER_UNIT_DIR/friday-host-agent.service"
activate_file "$BROKER_SERVICE_STAGE" "$SYSTEM_UNIT_DIR/friday-package-broker.service"
activate_file "$BROKER_SOCKET_STAGE" "$SYSTEM_UNIT_DIR/friday-package-broker.socket"
activate_file "$AGENT_DROPIN_STAGE" \
    "$USER_UNIT_DIR/friday-host-agent.service.d/20-deployment.conf"
activate_file "$SOCKET_DROPIN_STAGE" \
    "$SYSTEM_UNIT_DIR/friday-package-broker.socket.d/20-deployment.conf"
activate_file "$README_STAGE" /usr/local/share/doc/friday-host-control/README.md

/usr/bin/systemd-tmpfiles --create "$TMPFILES_CONFIG"
[ "$([ -d "$SOCKET_DIR" ] && /usr/bin/stat -c %u:%g:%a -- "$SOCKET_DIR")" \
    = "$USER_UID:$USER_GID:700" ] \
    || fail "stable host-agent socket directory has unsafe metadata"

/usr/bin/systemctl daemon-reload

if [ "$ENABLE_SERVICES" -eq 1 ]; then
    journal_write phase enabling
    LINGER_ATTEMPTED=1
    journal_write linger_attempted 1
    /usr/bin/loginctl enable-linger "$TARGET_USER"
    if [ "$USER_MANAGER_WAS_ACTIVE" -eq 0 ]; then
        USER_MANAGER_STARTED_BY_RUN=1
        journal_write user_manager_started_by_run 1
    fi
    /usr/bin/systemctl start "user@$USER_UID.service"
    BROKER_ENABLE_ATTEMPTED=1
    journal_write broker_enable_attempted 1
    /usr/bin/systemctl enable --now friday-package-broker.socket
    as_user_systemctl daemon-reload
    USER_ENABLE_ATTEMPTED=1
    journal_write user_enable_attempted 1
    as_user_systemctl enable --now friday-host-agent.service
    /usr/bin/systemctl is-active --quiet friday-package-broker.socket \
        || fail "package broker socket did not become active"
    as_user_systemctl is-active --quiet friday-host-agent.service \
        || fail "host-agent user service did not become active"
fi

journal_write phase committed
COMMITTED=1

printf '%s\n' \
    "Host Control files installed with backend features still disabled by default." \
    "Compose interpolation values:" \
    "  FRIDAY_HOST_RUNTIME_UID=$USER_UID" \
    "  FRIDAY_HOST_RUNTIME_GID=$USER_GID" \
    "  FRIDAY_HOST_AGENT_SOCKET_DIR_HOST=$SOCKET_DIR" \
    "  FRIDAY_HOST_AGENT_KEY_FILE_HOST=$AGENT_KEY" \
    "  FRIDAY_HOST_APPROVAL_SIGNING_KEY_FILE_HOST=$APPROVAL_SIGNING_KEY" \
    "  FRIDAY_HOST_APPROVAL_SIGNER_GID=$APPROVAL_SIGNER_GID" \
    "  FRIDAY_HOST_JOB_DATA_DIR_HOST=$FRIDAY_DATA_DIR/host-control/jobs" \
    "Enable only reviewed FRIDAY_HOST_* flags in the backend environment."
