#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

PROFILE_NAME=friday-engineer-backend
PROFILE_TARGET=/etc/apparmor.d/friday-engineer-backend
SECCOMP_DIRECTORY=/etc/friday-engineer
SECCOMP_TARGET=$SECCOMP_DIRECTORY/seccomp.json
APPARMOR_PARSER=/usr/sbin/apparmor_parser
PROFILE_CREATED=0
SECCOMP_CREATED=0
SECCOMP_DIRECTORY_CREATED=0
COMMITTED=0
TEMP_PATH=

fail() {
    printf '%s\n' "engineer AppArmor install: $*" >&2
    exit 1
}

cleanup() {
    [ -z "$TEMP_PATH" ] || [ ! -e "$TEMP_PATH" ] || /usr/bin/rm -f -- "$TEMP_PATH"
    if [ "$COMMITTED" -eq 0 ]; then
        [ "$PROFILE_CREATED" -eq 0 ] || /usr/bin/rm -f -- "$PROFILE_TARGET"
        [ "$SECCOMP_CREATED" -eq 0 ] || /usr/bin/rm -f -- "$SECCOMP_TARGET"
        [ "$SECCOMP_DIRECTORY_CREATED" -eq 0 ] \
            || /usr/bin/rmdir -- "$SECCOMP_DIRECTORY" 2>/dev/null \
            || :
    fi
}
trap cleanup EXIT HUP INT TERM

[ "$#" -eq 0 ] || fail "this installer accepts no arguments"
[ "$(/usr/bin/id -u)" -eq 0 ] || fail "run this explicit operator setup as root"

OS_ID=$(/usr/bin/sed -n 's/^ID=//p' /etc/os-release | /usr/bin/tr -d '"')
OS_VERSION=$(/usr/bin/sed -n 's/^VERSION_ID=//p' /etc/os-release | /usr/bin/tr -d '"')
[ "$OS_ID" = ubuntu ] || fail "this policy is supported only on Ubuntu"
case "$OS_VERSION" in
    2[4-9].*|[3-9][0-9].*) ;;
    *) fail "Ubuntu 24.04 or newer is required" ;;
esac

[ -f /sys/module/apparmor/parameters/enabled ] \
    && /usr/bin/grep -q '^Y' /sys/module/apparmor/parameters/enabled \
    || fail "AppArmor is not enabled"
[ -r /sys/kernel/security/apparmor/features/namespaces/userns_create ] \
    || fail "the kernel lacks AppArmor user-namespace mediation"
[ -r /sys/kernel/security/apparmor/features/namespaces/pivot_root ] \
    || fail "the kernel lacks AppArmor pivot_root mediation"

[ -x "$APPARMOR_PARSER" ] && [ ! -L "$APPARMOR_PARSER" ] \
    || fail "the fixed AppArmor parser is missing or symlinked"
[ "$(/usr/bin/stat -c %u -- "$APPARMOR_PARSER")" -eq 0 ] \
    || fail "the AppArmor parser is not root-owned"
if /usr/bin/find "$APPARMOR_PARSER" -prune -perm /022 -print | /usr/bin/grep -q .; then
    fail "the AppArmor parser is writable by group or other"
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
PROFILE_SOURCE=$SCRIPT_DIR/apparmor/friday-engineer-backend
SECCOMP_SOURCE=$SCRIPT_DIR/seccomp.json
[ -f "$PROFILE_SOURCE" ] && [ ! -L "$PROFILE_SOURCE" ] \
    || fail "the shipped profile is missing or symlinked"
[ "$(/usr/bin/readlink -f -- "$PROFILE_SOURCE")" = "$PROFILE_SOURCE" ] \
    || fail "the shipped profile path is not canonical"
[ -f "$SECCOMP_SOURCE" ] && [ ! -L "$SECCOMP_SOURCE" ] \
    || fail "the shipped seccomp profile is missing or symlinked"
[ "$(/usr/bin/readlink -f -- "$SECCOMP_SOURCE")" = "$SECCOMP_SOURCE" ] \
    || fail "the shipped seccomp profile path is not canonical"

# Compile against the running kernel feature set before touching /etc.
"$APPARMOR_PARSER" -Q -K -T "$PROFILE_SOURCE" \
    || fail "the shipped profile does not compile on this host"

if [ -e "$SECCOMP_DIRECTORY" ] || [ -L "$SECCOMP_DIRECTORY" ]; then
    [ -d "$SECCOMP_DIRECTORY" ] && [ ! -L "$SECCOMP_DIRECTORY" ] \
        && [ "$(/usr/bin/readlink -f -- "$SECCOMP_DIRECTORY")" = "$SECCOMP_DIRECTORY" ] \
        || fail "refusing an unsafe existing seccomp policy directory"
    [ "$(/usr/bin/stat -c %u:%g:%a -- "$SECCOMP_DIRECTORY")" = 0:0:755 ] \
        || fail "the seccomp policy directory must be root-owned 0755"
else
    /usr/bin/install -d -o root -g root -m 0755 "$SECCOMP_DIRECTORY"
    SECCOMP_DIRECTORY_CREATED=1
fi

if [ -e "$SECCOMP_TARGET" ] || [ -L "$SECCOMP_TARGET" ]; then
    [ -f "$SECCOMP_TARGET" ] && [ ! -L "$SECCOMP_TARGET" ] \
        || fail "refusing an unsafe existing seccomp policy path"
    [ "$(/usr/bin/stat -c %u:%g:%a:%h -- "$SECCOMP_TARGET")" = 0:0:644:1 ] \
        || fail "the installed seccomp policy metadata is unsafe"
    /usr/bin/cmp -s -- "$SECCOMP_SOURCE" "$SECCOMP_TARGET" \
        || fail "a different seccomp policy already exists; stop Friday and remove it explicitly"
else
    TEMP_PATH=$(/usr/bin/mktemp "$SECCOMP_DIRECTORY/.seccomp.json.XXXXXX")
    /usr/bin/install -o root -g root -m 0644 "$SECCOMP_SOURCE" "$TEMP_PATH"
    # Arm rollback before the atomic rename so a signal cannot strand a
    # partially committed policy between mv(1) and the next shell command.
    SECCOMP_CREATED=1
    /usr/bin/mv -f -- "$TEMP_PATH" "$SECCOMP_TARGET"
    TEMP_PATH=
fi

if [ -e "$PROFILE_TARGET" ] || [ -L "$PROFILE_TARGET" ]; then
    [ -f "$PROFILE_TARGET" ] && [ ! -L "$PROFILE_TARGET" ] \
        || fail "refusing an unsafe existing profile path"
    /usr/bin/cmp -s -- "$PROFILE_SOURCE" "$PROFILE_TARGET" \
        || fail "a different profile already exists; stop Friday and remove it explicitly"
else
    TEMP_PATH=$(/usr/bin/mktemp /etc/apparmor.d/.friday-engineer-backend.XXXXXX)
    /usr/bin/install -o root -g root -m 0644 "$PROFILE_SOURCE" "$TEMP_PATH"
    PROFILE_CREATED=1
    /usr/bin/mv -f -- "$TEMP_PATH" "$PROFILE_TARGET"
    TEMP_PATH=
fi

if ! "$APPARMOR_PARSER" -r -W "$PROFILE_TARGET"; then
    fail "the profile could not be loaded; no new policy file was retained"
fi

/usr/bin/grep -Fqx "$PROFILE_NAME (enforce)" /sys/kernel/security/apparmor/profiles \
    || fail "the loaded profile is not in enforce mode"

COMMITTED=1
printf '%s\n' \
    "Loaded enforcing AppArmor profile: $PROFILE_NAME" \
    "Installed root-owned seccomp profile: $SECCOMP_TARGET"
