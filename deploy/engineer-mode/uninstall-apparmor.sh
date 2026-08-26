#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

PROFILE_NAME=friday-engineer-backend
PROFILE_TARGET=/etc/apparmor.d/friday-engineer-backend
SECCOMP_DIRECTORY=/etc/friday-engineer
SECCOMP_TARGET=$SECCOMP_DIRECTORY/seccomp.json
APPARMOR_PARSER=/usr/sbin/apparmor_parser

fail() {
    printf '%s\n' "engineer AppArmor uninstall: $*" >&2
    exit 1
}

[ "$#" -eq 0 ] || fail "this uninstaller accepts no arguments"
[ "$(/usr/bin/id -u)" -eq 0 ] || fail "run this explicit operator removal as root"
[ -x "$APPARMOR_PARSER" ] || fail "the fixed AppArmor parser is unavailable"

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
SECCOMP_SOURCE=$SCRIPT_DIR/seccomp.json
if [ -e "$SECCOMP_TARGET" ] || [ -L "$SECCOMP_TARGET" ]; then
    [ -d "$SECCOMP_DIRECTORY" ] && [ ! -L "$SECCOMP_DIRECTORY" ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a -- "$SECCOMP_DIRECTORY")" = 0:0:755 ] \
        && [ -f "$SECCOMP_TARGET" ] && [ ! -L "$SECCOMP_TARGET" ] \
        && [ "$(/usr/bin/stat -c %u:%g:%a:%h -- "$SECCOMP_TARGET")" = 0:0:644:1 ] \
        && /usr/bin/cmp -s -- "$SECCOMP_SOURCE" "$SECCOMP_TARGET" \
        || fail "refusing an unsafe or different installed seccomp policy"
fi

if [ -e "$PROFILE_TARGET" ] || [ -L "$PROFILE_TARGET" ]; then
    [ -f "$PROFILE_TARGET" ] && [ ! -L "$PROFILE_TARGET" ] \
        || fail "refusing an unsafe profile path"
    "$APPARMOR_PARSER" -R "$PROFILE_TARGET" \
        || fail "profile removal failed; stop every container using it before retrying"
    /usr/bin/rm -f -- "$PROFILE_TARGET"
elif /usr/bin/grep -Fq "$PROFILE_NAME " /sys/kernel/security/apparmor/profiles; then
    fail "the profile is loaded but its policy file is missing; restore the release file first"
fi

if [ -e "$SECCOMP_TARGET" ] || [ -L "$SECCOMP_TARGET" ]; then
    /usr/bin/rm -f -- "$SECCOMP_TARGET"
    /usr/bin/rmdir -- "$SECCOMP_DIRECTORY" 2>/dev/null || :
fi

printf '%s\n' "Removed Engineer runtime policies: $PROFILE_NAME and $SECCOMP_TARGET"
