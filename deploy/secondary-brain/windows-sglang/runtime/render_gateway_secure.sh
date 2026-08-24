#!/bin/sh
set -eu
umask 077

read_secret() {
    secret_path=$1
    SECRET_VALUE=
    # The provisioner writes exactly 64 bytes with no line ending. A successful
    # read means a newline was present, including a seemingly harmless trailing
    # one, so reject it rather than ignoring additional file content.
    if IFS= read -r SECRET_VALUE < "$secret_path"; then
        exit 64
    fi
    case "$SECRET_VALUE" in
        *[!0-9a-f]*|'') exit 64 ;;
    esac
    test "${#SECRET_VALUE}" -eq 64
}

replace_all() {
    replace_value=$1
    replace_needle=$2
    replace_with=$3
    while :; do
        case "$replace_value" in
            *"$replace_needle"*)
                replace_prefix=${replace_value%%"$replace_needle"*}
                replace_suffix=${replace_value#*"$replace_needle"}
                replace_value=$replace_prefix$replace_with$replace_suffix
                ;;
            *)
                REPLACED_VALUE=$replace_value
                return
                ;;
        esac
    done
}

read_secret /run/friday-secrets/gateway-api-key
gateway_key=$SECRET_VALUE
read_secret /run/friday-secrets/sglang-api-key
upstream_key=$SECRET_VALUE
test "$gateway_key" != "$upstream_key"

PROFILE_ID=
if IFS= read -r PROFILE_ID < /run/friday-profile/id; then
    exit 64
fi
case "$PROFILE_ID" in
    *[!a-z0-9._-]*|'') exit 64 ;;
esac
test "${#PROFILE_ID}" -ge 3
test "${#PROFILE_ID}" -le 80

runtime_epoch_path=/run/friday-runtime-epoch/process-start-time-seconds
test -f "$runtime_epoch_path"
test ! -L "$runtime_epoch_path"
RUNTIME_EPOCH=
if IFS= read -r RUNTIME_EPOCH < "$runtime_epoch_path"; then
    exit 64
fi
case "$RUNTIME_EPOCH" in
    ''|[!1-9]*|*[!0-9.]*|*.*.*|*.) exit 64 ;;
esac
test "${#RUNTIME_EPOCH}" -le 32

set -- $(sha256sum /run/friday-profile/accepted.json)
PROFILE_SHA256=$1
test "$#" -eq 2
case "$PROFILE_SHA256" in
    *[!0-9a-f]*|'') exit 64 ;;
esac
test "${#PROFILE_SHA256}" -eq 64

# Shell builtins perform the substitution. No child process receives either
# bearer in argv or environment; only the mode-0600 tmpfs-rendered Nginx config
# contains them after provisioning.
while IFS= read -r config_line || test -n "$config_line"; do
    replace_all "$config_line" "__GATEWAY_BEARER__" "$gateway_key"
    config_line=$REPLACED_VALUE
    replace_all "$config_line" "__SGLANG_BEARER__" "$upstream_key"
    config_line=$REPLACED_VALUE
    replace_all "$config_line" "__PROFILE_ID__" "$PROFILE_ID"
    config_line=$REPLACED_VALUE
    replace_all "$config_line" "__PROFILE_SHA256__" "$PROFILE_SHA256"
    config_line=$REPLACED_VALUE
    printf '%s\n' "$config_line"
done < /etc/friday-gateway/gateway.conf.template > /tmp/gateway.conf

unset SECRET_VALUE gateway_key upstream_key PROFILE_ID PROFILE_SHA256 RUNTIME_EPOCH
unset replace_value replace_needle replace_with
unset replace_prefix replace_suffix REPLACED_VALUE config_line secret_path runtime_epoch_path
exec nginx -c /tmp/gateway.conf -g 'daemon off;'
