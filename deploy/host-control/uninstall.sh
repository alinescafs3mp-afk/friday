#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
TARGET_USER=
PURGE_SECRETS=0

fail() {
    printf '%s\n' "host-control uninstall: $*" >&2
    exit 1
}

usage() {
    printf '%s\n' \
        "Usage: uninstall.sh --user USER [--purge-secrets]" \
        "Host-job evidence is always preserved. Linger is never disabled because" \
        "other user services may depend on it."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user)
            [ "$#" -ge 2 ] || fail "--user requires a value"
            TARGET_USER=$2
            shift 2
            ;;
        --purge-secrets)
            PURGE_SECRETS=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) fail "unknown argument: $1" ;;
    esac
done

[ "$(/usr/bin/id -u)" -eq 0 ] || fail "run this explicit operator removal as root"
case "$TARGET_USER" in
    *[!a-z0-9_-]*|'') fail "a valid --user is required" ;;
esac
USER_UID=$(/usr/bin/id -u -- "$TARGET_USER" 2>/dev/null) || fail "selected user does not exist"
USER_HOME=$(/usr/bin/getent passwd "$TARGET_USER" | /usr/bin/cut -d: -f6)

as_user_systemctl() {
    /usr/sbin/runuser -u "$TARGET_USER" -- /usr/bin/env -i \
        HOME="$USER_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
        PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
        /usr/bin/systemctl --user "$@"
}

if [ -S "/run/user/$USER_UID/bus" ]; then
    if as_user_systemctl is-active --quiet friday-host-agent.service; then
        as_user_systemctl stop friday-host-agent.service \
            || fail "host agent did not stop; no files were removed"
    fi
    as_user_systemctl disable friday-host-agent.service || true
fi
if /usr/bin/systemctl is-active --quiet friday-package-broker.socket; then
    /usr/bin/systemctl stop friday-package-broker.socket \
        || fail "package broker socket did not stop; no files were removed"
fi
/usr/bin/systemctl disable friday-package-broker.socket || true
if /usr/bin/systemctl is-active --quiet friday-package-broker.service; then
    /usr/bin/systemctl stop friday-package-broker.service \
        || fail "package broker did not drain and stop; no files were removed"
fi

/usr/bin/rm -f -- \
    /etc/tmpfiles.d/friday-host-agent.conf \
    /etc/systemd/user/friday-host-agent.service \
    /etc/systemd/user/friday-host-agent.service.d/20-deployment.conf \
    /etc/systemd/system/friday-package-broker.service \
    /etc/systemd/system/friday-package-broker.socket \
    /etc/systemd/system/friday-package-broker.socket.d/20-deployment.conf \
    /usr/local/share/doc/friday-host-control/README.md
/usr/bin/rm -f -- "/run/friday-host-agent/$USER_UID/agent.sock"
/usr/bin/rmdir --ignore-fail-on-non-empty \
    "/run/friday-host-agent/$USER_UID" /run/friday-host-agent || true
/usr/bin/rmdir --ignore-fail-on-non-empty \
    /etc/systemd/user/friday-host-agent.service.d \
    /etc/systemd/system/friday-package-broker.socket.d \
    /usr/local/share/doc/friday-host-control || true

# Remove only this fixed installation tree, and reject a substituted symlink.
if [ -d /opt/friday-host-control ] && [ ! -L /opt/friday-host-control ]; then
    /usr/bin/find /opt/friday-host-control -xdev -depth -delete
fi

if [ "$PURGE_SECRETS" -eq 1 ]; then
    [ ! -L /etc/friday-host-control ] || fail "refusing symlinked control directory"
    /usr/bin/rm -f -- \
        /etc/friday-host-control/host-agent.env \
        /etc/friday-host-control/host-agent-policy.toml \
        /etc/friday-host-control/release.env \
        /etc/friday-host-control/broker-policy.toml \
        /etc/friday-host-control/broker.key \
        /etc/friday-host-control/broker-signing.key \
        /etc/friday-host-control/broker-signing.pub \
        /etc/friday-host-control/backend-approval-signing.key \
        /etc/friday-host-control/backend-approval-signing.pub
    [ ! -L "$USER_HOME/.config/friday-host-agent/agent.key" ] \
        || fail "refusing symlinked host-agent key"
    /usr/bin/rm -f -- "$USER_HOME/.config/friday-host-agent/agent.key"
    /usr/bin/rmdir --ignore-fail-on-non-empty \
        "$USER_HOME/.config/friday-host-agent" /etc/friday-host-control || true
fi

/usr/bin/systemctl daemon-reload
if [ -S "/run/user/$USER_UID/bus" ]; then
    as_user_systemctl daemon-reload || true
fi

printf '%s\n' \
    "Host Control services and binaries removed." \
    "Host-job evidence and package-broker state were preserved." \
    "Backend FRIDAY_HOST_* flags and the Compose override must be removed separately."
