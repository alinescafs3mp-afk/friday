#!/bin/sh
set -eu

read_secret() {
    secret_path=$1
    SECRET_VALUE=
    IFS= read -r SECRET_VALUE < "$secret_path" || test -n "$SECRET_VALUE"
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

# Shell builtins perform the substitution. No child process receives either
# bearer in argv or environment; only the private tmpfs-rendered Nginx config
# contains them after provisioning.
while IFS= read -r config_line || test -n "$config_line"; do
    replace_all "$config_line" "__GATEWAY_BEARER__" "$gateway_key"
    config_line=$REPLACED_VALUE
    replace_all "$config_line" "__SGLANG_BEARER__" "$upstream_key"
    config_line=$REPLACED_VALUE
    printf '%s\n' "$config_line"
done < /etc/friday-gateway/gateway.conf.template > /tmp/gateway.conf

unset SECRET_VALUE gateway_key upstream_key replace_value replace_needle replace_with
unset replace_prefix replace_suffix REPLACED_VALUE config_line secret_path
exec nginx -c /tmp/gateway.conf -g 'daemon off;'
