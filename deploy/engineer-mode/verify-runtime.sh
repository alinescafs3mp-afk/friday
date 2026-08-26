#!/bin/sh
set -eu

PATH=/usr/bin:/bin
export PATH

CONTAINER=${1:-jericho-backend}
case "$CONTAINER" in
    *[!A-Za-z0-9_.-]*|'')
        printf '%s\n' "invalid container name" >&2
        exit 2
        ;;
esac

fail() {
    printf '%s\n' "engineer runtime smoke: $*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker client is unavailable"
DAEMON_SECURITY_OPTIONS=$(docker info --format '{{range .SecurityOptions}}{{println .}}{{end}}')
if printf '%s\n' "$DAEMON_SECURITY_OPTIONS" | /usr/bin/grep -Fqi rootless; then
    fail "rootless Docker is outside the accepted boundary"
fi
[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER")" = true ] \
    || fail "backend container is not running"
[ "$(docker inspect --format '{{.AppArmorProfile}}' "$CONTAINER")" = friday-engineer-backend ] \
    || fail "the enforcing Friday AppArmor profile is not attached"
[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$CONTAINER")" = true ] \
    || fail "the backend root filesystem is writable"
[ "$(docker inspect --format '{{.HostConfig.Privileged}}' "$CONTAINER")" = false ] \
    || fail "the backend is privileged"
[ "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "$CONTAINER")" = 512 ] \
    || fail "the backend PID cgroup limit is not 512"
[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$CONTAINER")" = '["ALL"]' ] \
    || fail "the backend did not drop all Linux capabilities"
CAP_ADD=$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$CONTAINER")
[ "$CAP_ADD" = null ] || [ "$CAP_ADD" = '[]' ] \
    || fail "the backend gained an added Linux capability"
[ "$(docker exec -i "$CONTAINER" /usr/bin/id -u)" -ne 0 ] \
    || fail "the live backend runs as root"

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
PROFILE_SOURCE=$SCRIPT_DIR/apparmor/friday-engineer-backend
PROFILE_TARGET=/etc/apparmor.d/friday-engineer-backend
[ -f "$PROFILE_SOURCE" ] && [ ! -L "$PROFILE_SOURCE" ] \
    && [ -f "$PROFILE_TARGET" ] && [ ! -L "$PROFILE_TARGET" ] \
    && /usr/bin/cmp -s -- "$PROFILE_SOURCE" "$PROFILE_TARGET" \
    || fail "the installed AppArmor policy differs from this release"

SECURITY_OPTIONS=$(docker inspect --format '{{range .HostConfig.SecurityOpt}}{{println .}}{{end}}' "$CONTAINER")
printf '%s\n' "$SECURITY_OPTIONS" | /usr/bin/grep -Eq '^no-new-privileges(:true)?$' \
    || fail "no-new-privileges is absent"
SECCOMP_OPTION=
while IFS= read -r OPTION; do
    case "$OPTION" in
        seccomp=*)
            [ -z "$SECCOMP_OPTION" ] || fail "multiple seccomp policies are reported"
            SECCOMP_OPTION=${OPTION#seccomp=}
            ;;
    esac
done <<EOF
$SECURITY_OPTIONS
EOF
[ -n "$SECCOMP_OPTION" ] || fail "the shipped seccomp policy is absent"
if [ "$SECCOMP_OPTION" = unconfined ]; then
    fail "seccomp=unconfined is forbidden"
fi
case "$SECCOMP_OPTION" in
    /*) ;;
    *) fail "the seccomp policy path is not canonical" ;;
esac
SECCOMP_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$SECCOMP_OPTION")" && pwd -P) \
    || fail "the selected seccomp policy directory is unavailable"
SECCOMP_SELECTED=$SECCOMP_DIR/$(/usr/bin/basename -- "$SECCOMP_OPTION")
SECCOMP_SOURCE=$SCRIPT_DIR/seccomp.json
SECCOMP_TARGET=/etc/friday-engineer/seccomp.json
[ "$SECCOMP_OPTION" = "$SECCOMP_SELECTED" ] \
    && [ "$SECCOMP_SELECTED" = "$SECCOMP_TARGET" ] \
    && [ "$(/usr/bin/stat -c %u:%g:%a -- "$SECCOMP_DIR")" = 0:0:755 ] \
    && [ -f "$SECCOMP_SELECTED" ] && [ ! -L "$SECCOMP_SELECTED" ] \
    && [ "$(/usr/bin/stat -c %u:%g:%a:%h -- "$SECCOMP_SELECTED")" = 0:0:644:1 ] \
    && /usr/bin/cmp -s -- "$SECCOMP_SOURCE" "$SECCOMP_SELECTED" \
    || fail "the selected seccomp policy is not this release's exact profile"

STATUS=$(docker exec -i "$CONTAINER" /usr/bin/sed -n \
    -e '/^CapEff:/p' -e '/^NoNewPrivs:/p' -e '/^Seccomp:/p' /proc/1/status)
printf '%s\n' "$STATUS" | /usr/bin/grep -Eq '^CapEff:[[:space:]]+0000000000000000$' \
    || fail "the live backend has an effective capability"
printf '%s\n' "$STATUS" | /usr/bin/grep -Eq '^NoNewPrivs:[[:space:]]+1$' \
    || fail "the live backend lacks no-new-privileges"
printf '%s\n' "$STATUS" | /usr/bin/grep -Eq '^Seccomp:[[:space:]]+2$' \
    || fail "the live backend is not under a seccomp filter"

[ "$(docker exec -i "$CONTAINER" /usr/bin/cat /proc/self/attr/current)" = \
    'friday-engineer-backend (enforce)' ] \
    || fail "the live backend is not under the enforcing AppArmor profile"
[ "$(docker exec -i "$CONTAINER" /usr/bin/python3 -c \
    'from friday.organs.engineer.sandbox import smoke_preflight; r=smoke_preflight(); print("ok" if r.get("ok") is True and r.get("boundary") == "bubblewrap" and r.get("network") == "none" and r.get("network_namespace") == "isolated" and r.get("external_interfaces") == 0 and r.get("external_routes") == 0 and r.get("ipv4_connectivity") == "blocked" and r.get("ipv6_connectivity") == "blocked" else "closed")')" = ok ] \
    || fail "the real no-network bubblewrap smoke did not pass"

printf '%s\n' "Engineer container boundary passed: AppArmor + seccomp + no-new-privileges + cap-drop + PID limit + bubblewrap."
