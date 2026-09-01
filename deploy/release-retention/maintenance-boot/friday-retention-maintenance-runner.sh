#!/bin/sh
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

CONFIG=/etc/friday-retention-maintenance
PREMOUNT=/run/friday-retention/maintenance-premount-receipt.v1.json
AUTHORITY=/run/friday-retention/maintenance-premount-authority.v1.json
ROOT_DEVICE_NODE=/run/friday-retention/ordinary-root-device
ROOT_DEVICE_PROBE=/run/friday-retention/ordinary-root-device.probe
SYSROOT=/sysroot
RDINIT=/usr/libexec/friday/release_artifact_retention_maintenance_launcher

stop_boot() {
    rm -f -- "$AUTHORITY" "$PREMOUNT" 2>/dev/null || :
    sync 2>/dev/null || :
    umount -R "$SYSROOT" 2>/dev/null || :
    while :; do
        poweroff -f 2>/dev/null || reboot -f 2>/dev/null || :
        sleep 1
    done
}
fail() {
    printf '%s\n' friday_retention_maintenance_failed_closed >&2
    stop_boot
}
reboot_ordinary() {
    rm -f -- "$AUTHORITY" "$PREMOUNT" 2>/dev/null || fail
    sync || fail
    umount -R "$SYSROOT" || fail
    while :; do
        reboot -f 2>/dev/null || poweroff -f 2>/dev/null || :
        sleep 1
    done
}
digest() {
    value=$(sha256sum -- "$1" | cut -d ' ' -f 1) || fail
    hex64 "$value"
    printf '%s' "$value"
}
digest_text() {
    value=$(printf '%s' "$1" | sha256sum | cut -d ' ' -f 1) || fail
    hex64 "$value"
    printf '%s' "$value"
}
hex64() {
    [ "${#1}" -eq 64 ] || fail
    case "$1" in ''|*[!0-9a-f]*) fail ;; esac
}
decimal() {
    case "$1" in ''|*[!0-9]*) fail ;; esac
}
filesystem_uuid() {
    case "$1" in
        ????????-????-????-????-????????????) ;;
        *) fail ;;
    esac
    case "$1" in ''|*[!0-9a-f-]*) fail ;; esac
    [ "${#1}" -eq 36 ] || fail
}
module_chain() {
    [ "$1" = - ] && return 0
    case "$1" in ''|:*|*:|*::*|*[!A-Za-z0-9_:-]*) fail ;; esac
    old_ifs=$IFS
    IFS=:
    set -- $1
    IFS=$old_ifs
    [ "$#" -gt 0 ] && [ "$#" -le 128 ] || fail
    seen_modules=:
    for module in "$@"; do
        case "$module" in ''|*[!A-Za-z0-9_-]*) fail ;; esac
        case "$seen_modules" in *:"$module":*) fail ;; esac
        seen_modules=$seen_modules$module:
    done
}
load_root_block_modules() {
    module_started_at=$(date +%s) || fail
    decimal "$module_started_at"
    module_deadline=$((module_started_at + 60))
    [ "$ROOT_BLOCK_MODULE_CHAIN" = - ] || {
        old_ifs=$IFS
        IFS=:
        set -- $ROOT_BLOCK_MODULE_CHAIN
        IFS=$old_ifs
        for module in "$@"; do
            module_now=$(date +%s) || fail
            decimal "$module_now"
            module_remaining=$((module_deadline - module_now))
            [ "$module_remaining" -gt 0 ] || fail
            timeout -s KILL "$module_remaining" modprobe "$module" || fail
        done
    }
    # The retention engine accepts only ext4.  modprobe also succeeds when
    # ext4 is built into the exact reviewed kernel.
    module_now=$(date +%s) || fail
    decimal "$module_now"
    module_remaining=$((module_deadline - module_now))
    [ "$module_remaining" -gt 0 ] || fail
    timeout -s KILL "$module_remaining" modprobe ext4 || fail
}
verify_root_block_module_chain() {
    verified_device_id=$1
    for slave_path in "/sys/dev/block/$verified_device_id"/slaves/*; do
        [ ! -e "$slave_path" ] || fail
    done
    block_path=$(readlink -f "/sys/dev/block/$verified_device_id") || fail
    case "$block_path" in /sys/devices/*) ;; *) fail ;; esac
    cursor=$block_path
    observed_chain=
    observed_modules=:
    driver_seen=0
    while [ "$cursor" != /sys/devices ] && [ "$cursor" != / ]; do
        if [ -L "$cursor/driver" ]; then
            driver_seen=1
        fi
        if [ -L "$cursor/driver/module" ]; then
            module_path=$(readlink -f "$cursor/driver/module") || fail
            module=${module_path##*/}
            case "$module" in ''|*[!A-Za-z0-9_-]*) fail ;; esac
            case "$observed_modules" in
                *:"$module":*) ;;
                *)
                    observed_modules=$observed_modules$module:
                    if [ -n "$observed_chain" ]; then
                        observed_chain=$observed_chain:$module
                    else
                        observed_chain=$module
                    fi
                    ;;
            esac
        fi
        cursor=${cursor%/*}
        [ -n "$cursor" ] || cursor=/
    done
    [ "$driver_seen" -eq 1 ] || fail
    [ "$(readlink -f "/sys/dev/block/$verified_device_id")" = "$block_path" ] || fail
    [ -n "$observed_chain" ] || observed_chain=-
    [ "$observed_chain" = "$ROOT_BLOCK_MODULE_CHAIN" ] || fail
}
resolve_root_device() {
    started_at=$(date +%s) || fail
    decimal "$started_at"
    deadline=$((started_at + 120))
    attempt=0
    stable_count=0
    previous_inventory=
    while [ "$attempt" -lt 120 ]; do
        now=$(date +%s) || fail
        decimal "$now"
        [ "$now" -lt "$deadline" ] || fail
        attempt=$((attempt + 1))
        device_count=0
        match_count=0
        matched_device_id=
        block_inventory=
        rm -f -- "$ROOT_DEVICE_PROBE" || fail
        for sys_device in /sys/class/block/*; do
            now=$(date +%s) || fail
            decimal "$now"
            [ "$now" -lt "$deadline" ] || fail
            [ -d "$sys_device" ] || continue
            [ -f "$sys_device/dev" ] || fail
            device_count=$((device_count + 1))
            [ "$device_count" -le 4096 ] || fail
            device_id=$(cat "$sys_device/dev") || fail
            case "$device_id" in
                *:*) device_major=${device_id%:*}; device_minor=${device_id#*:} ;;
                *) fail ;;
            esac
            decimal "$device_major"
            decimal "$device_minor"
            [ "$device_id" = "$device_major:$device_minor" ] || fail
            block_name=${sys_device##*/}
            case "$block_name" in ''|*[!A-Za-z0-9_.-]*) fail ;; esac
            block_inventory=$block_inventory$block_name:$device_id\;
            mknod "$ROOT_DEVICE_PROBE" b "$device_major" "$device_minor" || fail
            chmod 0600 "$ROOT_DEVICE_PROBE" || fail
            if candidate_uuid=$(timeout -s KILL 2 blkid -p -c /dev/null -s UUID \
                -o value "$ROOT_DEVICE_PROBE" 2>/dev/null); then
                probe_status=0
            else
                probe_status=$?
                candidate_uuid=
            fi
            rm -f -- "$ROOT_DEVICE_PROBE" || fail
            case "$probe_status" in 0|2) ;; *) fail ;; esac
            [ "${#candidate_uuid}" -le 64 ] || fail
            if [ "$candidate_uuid" = "$ORDINARY_ROOT_FILESYSTEM_UUID" ]; then
                match_count=$((match_count + 1))
                matched_device_id=$device_id
            fi
        done
        [ "$match_count" -le 1 ] || fail

        scsi_quiet=1
        for scsi_host in /sys/class/scsi_host/*; do
            [ -d "$scsi_host" ] || continue
            [ -f "$scsi_host/host_busy" ] && [ -f "$scsi_host/state" ] || fail
            host_busy=$(cat "$scsi_host/host_busy") || fail
            host_state=$(cat "$scsi_host/state") || fail
            decimal "$host_busy"
            [ "$host_busy" -eq 0 ] && [ "$host_state" = running ] || scsi_quiet=0
        done
        if [ "$block_inventory" = "$previous_inventory" ]; then
            stable_count=$((stable_count + 1))
        else
            previous_inventory=$block_inventory
            stable_count=1
        fi
        if [ "$device_count" -gt 0 ] && [ "$match_count" -eq 1 ] \
            && [ "$scsi_quiet" -eq 1 ] && [ "$stable_count" -ge 5 ]; then
            ORDINARY_ROOT_DEVICE_ID=$matched_device_id
            ROOT_MAJOR=${ORDINARY_ROOT_DEVICE_ID%:*}
            ROOT_MINOR=${ORDINARY_ROOT_DEVICE_ID#*:}
            verify_root_block_module_chain "$ORDINARY_ROOT_DEVICE_ID"
            mknod "$ROOT_DEVICE_NODE" b "$ROOT_MAJOR" "$ROOT_MINOR" || fail
            chmod 0600 "$ROOT_DEVICE_NODE" || fail
            root_hex=$(stat -c '%t:%T' "$ROOT_DEVICE_NODE") || fail
            root_major_hex=${root_hex%:*}
            root_minor_hex=${root_hex#*:}
            case "$root_major_hex:$root_minor_hex" in
                *[!0-9a-fA-F:]*) fail ;;
            esac
            [ "$((0x$root_major_hex)):$((0x$root_minor_hex))" \
                = "$ORDINARY_ROOT_DEVICE_ID" ] || fail
            [ "$(timeout -s KILL 2 blkid -p -c /dev/null -s UUID -o value \
                "$ROOT_DEVICE_NODE" 2>/dev/null)" \
                = "$ORDINARY_ROOT_FILESYSTEM_UUID" ] || fail
            [ "$(timeout -s KILL 2 blkid -p -c /dev/null -s TYPE -o value \
                "$ROOT_DEVICE_NODE" 2>/dev/null)" \
                = ext4 ] || fail
            return 0
        fi
        sleep 1
    done
    fail
}
read_config() {
    value=$(cat "$CONFIG/$1") || fail
    [ -n "$value" ] || fail
    case "$value" in *[!A-Za-z0-9_./:-]*) fail ;; esac
    printf '%s' "$value"
}
validate_root_file() {
    path=$1 expected=$2 mode=$3
    [ -f "$path" ] && [ ! -L "$path" ] || fail
    [ "$(stat -c %u "$path")" -eq 0 ] || fail
    [ "$(stat -c %a "$path")" = "$mode" ] || fail
    [ "$(stat -c %h "$path")" -eq 1 ] || fail
    [ "$(digest "$path")" = "$expected" ] || fail
}
proc_starttime() {
    stat_line=$(cat "$1/stat") || fail
    case "$stat_line" in *') '*) ;; *) fail ;; esac
    stat_tail=${stat_line##*) }
    set -- $stat_tail
    [ "$#" -ge 20 ] || fail
    shift 19
    decimal "$1"
    [ "$1" -gt 0 ] || fail
    printf '%s' "$1"
}
kthread_value() {
    found=0
    result=
    while IFS=':' read -r name value; do
        [ "$name" = Kthread ] || continue
        found=$((found + 1))
        set -- $value
        [ "$#" -eq 1 ] || fail
        case "$1" in 0|1) result=$1 ;; *) fail ;; esac
    done <"$1/status"
    [ "$found" -eq 1 ] || fail
    printf '%s' "$result"
}
capture_process_epoch() {
    process_projection=
    process_count=0
    task_count=0
    pid1_seen=0
    pid1_starttime=
    for process_path in /proc/[0-9]*; do
        [ -d "$process_path" ] || fail
        pid=${process_path##*/}
        decimal "$pid"
        [ "$pid" -gt 0 ] || fail
        process_count=$((process_count + 1))
        [ "$process_count" -le 131072 ] || fail
        process_kthread=$(kthread_value "$process_path") || fail
        if [ "$pid" -eq 1 ]; then
            [ "$process_kthread" -eq 0 ] || fail
            pid1_seen=$((pid1_seen + 1))
        else
            [ "$process_kthread" -eq 1 ] || fail
        fi
        process_task_count=0
        for task_path in "$process_path"/task/[0-9]*; do
            [ -d "$task_path" ] || fail
            tid=${task_path##*/}
            decimal "$tid"
            [ "$tid" -gt 0 ] || fail
            task_kthread=$(kthread_value "$task_path") || fail
            [ "$task_kthread" = "$process_kthread" ] || fail
            starttime=$(proc_starttime "$task_path") || fail
            namespace=$(readlink "$task_path/ns/mnt" 2>/dev/null || printf '%s' absent)
            case "$namespace" in absent|mnt:\[*\]) ;; *) fail ;; esac
            process_projection=${process_projection}${pid}:${tid}:${starttime}:${namespace}\;
            process_task_count=$((process_task_count + 1))
            task_count=$((task_count + 1))
            [ "$task_count" -le 262144 ] || fail
            if [ "$pid" -eq 1 ]; then
                [ "$tid" -eq 1 ] || fail
                pid1_starttime=$starttime
            fi
        done
        [ "$process_task_count" -gt 0 ] || fail
    done
    [ "$pid1_seen" -eq 1 ] && [ -n "$pid1_starttime" ] || fail
    PID1_STARTTIME_SHA256=$(digest_text "$pid1_starttime") || fail
    PROCESS_EPOCH_SHA256=$(
        digest_text "friday-maintenance-process-v1:$BOOT_ID_SHA256:$process_projection"
    ) || fail
}
parse_mountinfo() {
    mountinfo_file=$1
    mount_count=0
    proc_count=0
    seen_mount_ids=' '
    while IFS= read -r line; do
        [ -n "$line" ] || fail
        set -- $line
        [ "$#" -ge 10 ] || fail
        mount_id=$1
        parent_id=$2
        device_id=$3
        root_path=$4
        mountpoint=$5
        decimal "$mount_id"
        decimal "$parent_id"
        case "$device_id" in
            *:*) major=${device_id%:*}; minor=${device_id#*:} ;;
            *) fail ;;
        esac
        decimal "$major"
        decimal "$minor"
        case "$seen_mount_ids" in *" $mount_id "*) fail ;; esac
        seen_mount_ids=${seen_mount_ids}${mount_id}' '
        shift 6
        while [ "$#" -gt 0 ] && [ "$1" != - ]; do shift; done
        [ "$#" -eq 4 ] && [ "$1" = - ] || fail
        shift
        filesystem=$1
        source=$2
        [ -n "$filesystem" ] && [ -n "$source" ] || fail
        [ "$device_id" != "$ORDINARY_ROOT_DEVICE_ID" ] || fail
        case "$filesystem:$root_path:$source" in
            nsfs:*|*:mnt:\[*\]:*|*:*:mnt:\[*\]) fail ;;
        esac
        if [ "$mountpoint" = /proc ]; then
            [ "$filesystem" = proc ] || fail
            proc_count=$((proc_count + 1))
        fi
        mount_count=$((mount_count + 1))
        [ "$mount_count" -le 262144 ] || fail
    done <"$mountinfo_file"
    [ "$mount_count" -gt 0 ] && [ "$proc_count" -eq 1 ] || fail
}
validate_namespace_epoch() {
    mount_namespace_id=$(stat -Lc '%d:%i' /proc/1/ns/mnt) || fail
    case "$mount_namespace_id" in
        *:*) ns_device=${mount_namespace_id%:*}; ns_inode=${mount_namespace_id#*:} ;;
        *) fail ;;
    esac
    decimal "$ns_device"
    decimal "$ns_inode"
    MOUNT_NAMESPACE_SHA256=$(digest_text "$mount_namespace_id") || fail
    mountinfo_before=$(digest /proc/1/mountinfo) || fail
    parse_mountinfo /proc/1/mountinfo
    mountinfo_after=$(digest /proc/1/mountinfo) || fail
    [ "$mountinfo_before" = "$mountinfo_after" ] || fail

    namespace_task_count=0
    for process_path in /proc/[0-9]*; do
        [ -d "$process_path" ] || fail
        for task_path in "$process_path"/task/[0-9]*; do
            [ -d "$task_path" ] || fail
            namespace_task_count=$((namespace_task_count + 1))
            namespace=$(readlink "$task_path/ns/mnt" 2>/dev/null || printf '%s' absent)
            if [ "$namespace" != absent ]; then
                [ "$(stat -Lc '%d:%i' "$task_path/ns/mnt")" = "$mount_namespace_id" ] || fail
                [ "$(digest "$task_path/mountinfo")" = "$mountinfo_before" ] || fail
            fi
            for fd_path in "$task_path"/fd/[0-9]*; do
                [ -L "$fd_path" ] || continue
                fd_target=$(readlink "$fd_path") || fail
                case "$fd_target" in
                    mnt:\[*\]|anon_inode:\[io_uring\]) fail ;;
                esac
            done
        done
    done
    [ "$namespace_task_count" -gt 0 ] || fail
    MOUNTINFO_SHA256=$mountinfo_before
    NAMESPACE_EPOCH_SHA256=$(digest_text \
        "friday-maintenance-namespace-v1:$MOUNT_NAMESPACE_SHA256:$MOUNTINFO_SHA256:$PROCESS_EPOCH_SHA256") || fail
}
capture_premount_fixed_point() {
    previous=
    attempt=0
    while [ "$attempt" -lt 8 ]; do
        attempt=$((attempt + 1))
        capture_process_epoch
        process_before=$PROCESS_EPOCH_SHA256
        pid1_before=$PID1_STARTTIME_SHA256
        validate_namespace_epoch
        capture_process_epoch
        [ "$PROCESS_EPOCH_SHA256" = "$process_before" ] || continue
        [ "$PID1_STARTTIME_SHA256" = "$pid1_before" ] || continue
        current=$PROCESS_EPOCH_SHA256:$NAMESPACE_EPOCH_SHA256:$PID1_STARTTIME_SHA256:$MOUNT_NAMESPACE_SHA256:$MOUNTINFO_SHA256
        if [ -n "$previous" ] && [ "$current" = "$previous" ]; then
            return 0
        fi
        previous=$current
    done
    fail
}

[ "$$" -eq 1 ] || fail
[ -d "$CONFIG" ] && [ ! -L "$CONFIG" ] || fail
mkdir -p /dev /proc /run /sys "$SYSROOT" || fail
mount -t proc -o nosuid,nodev,noexec proc /proc || fail
mount -t sysfs -o nosuid,nodev,noexec sysfs /sys || fail
mount -t devtmpfs -o nosuid,noexec,mode=0755 devtmpfs /dev || fail
mount -t tmpfs -o nosuid,nodev,mode=0755 tmpfs /run || fail
mkdir -p /run/friday-retention "$SYSROOT" || fail
chmod 0700 /run/friday-retention || fail
[ -w /proc/sys/kernel/io_uring_disabled ] || fail
printf '%s\n' 2 > /proc/sys/kernel/io_uring_disabled || fail
[ "$(cat /proc/sys/kernel/io_uring_disabled)" = 2 ] || fail
[ ! -e "$PREMOUNT" ] && [ ! -L "$PREMOUNT" ] || fail
[ ! -e "$AUTHORITY" ] && [ ! -L "$AUTHORITY" ] || fail

[ "$(id -u)" -eq 0 ] || fail
TRANSACTION=$(read_config transaction-id)
REQUEST_PATH=$(read_config request-path)
ROOT_REQUEST_PATH=$(read_config root-request-path)
REQUEST_FILE_SHA256=$(read_config request-file-sha256)
REQUEST_SHA256=$(read_config request-sha256)
IMAGE_AUTHORITY_PATH=$(read_config image-authority-path)
IMAGE_AUTHORITY_SHA256=$(read_config image-authority-sha256)
CONTROLLER_PATH=$(read_config controller-path)
CONTROLLER_SHA256=$(read_config controller-sha256)
OWNER_USER=$(read_config owner-user)
OWNER_UID=$(read_config owner-uid)
MAINTENANCE_CMDLINE_TEMPLATE_SHA256=$(read_config maintenance-cmdline-sha256)
REVIEWED_ORDINARY_ROOT_DEVICE_ID=$(read_config ordinary-root-device-id)
ORDINARY_ROOT_FILESYSTEM_UUID=$(read_config ordinary-root-filesystem-uuid)
ROOT_BLOCK_MODULE_CHAIN=$(read_config root-block-module-chain)
for value in "$TRANSACTION" "$REQUEST_FILE_SHA256" "$REQUEST_SHA256" \
    "$IMAGE_AUTHORITY_SHA256" "$CONTROLLER_SHA256" \
    "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256"
do
    hex64 "$value"
done
case "$REQUEST_PATH:$ROOT_REQUEST_PATH:$IMAGE_AUTHORITY_PATH:$CONTROLLER_PATH" in
    *[!A-Za-z0-9_./:-]*|*..*) fail ;;
esac
for path in "$REQUEST_PATH" "$ROOT_REQUEST_PATH" "$IMAGE_AUTHORITY_PATH" "$CONTROLLER_PATH"; do
    case "$path" in /*) ;; *) fail ;; esac
done
case "$OWNER_USER" in ''|*[!A-Za-z0-9_.-]*|root) fail ;; esac
decimal "$OWNER_UID"
[ "$OWNER_UID" -gt 0 ] || fail
case "$REVIEWED_ORDINARY_ROOT_DEVICE_ID" in
    *:*)
        REVIEWED_ROOT_MAJOR=${REVIEWED_ORDINARY_ROOT_DEVICE_ID%:*}
        REVIEWED_ROOT_MINOR=${REVIEWED_ORDINARY_ROOT_DEVICE_ID#*:}
        ;;
    *) fail ;;
esac
decimal "$REVIEWED_ROOT_MAJOR"
decimal "$REVIEWED_ROOT_MINOR"
[ "$REVIEWED_ORDINARY_ROOT_DEVICE_ID" = "$REVIEWED_ROOT_MAJOR:$REVIEWED_ROOT_MINOR" ] \
    || fail
filesystem_uuid "$ORDINARY_ROOT_FILESYSTEM_UUID"
module_chain "$ROOT_BLOCK_MODULE_CHAIN"

CMDLINE=$(cat /proc/cmdline) || fail
[ -n "$CMDLINE" ] || fail
tab=$(printf '\t')
carriage_return=$(printf '\r')
case "$CMDLINE" in
    ' '*|*' '|*'  '*|*"$tab"*|*"$carriage_return"*) fail ;;
esac
marker=
initrd_token=
marker_count=0
initrd_count=0
rdinit_count=0
retain_count=0
io_uring_count=0
PROJECTED_CMDLINE=
append_projected_token() {
    if [ -n "$PROJECTED_CMDLINE" ]; then
        PROJECTED_CMDLINE=$PROJECTED_CMDLINE' '$1
    else
        PROJECTED_CMDLINE=$1
    fi
}
for token in $CMDLINE; do
    case "$token" in
        rd.friday.retention=*)
            marker=${token#rd.friday.retention=}
            marker_count=$((marker_count + 1))
            append_projected_token "$token"
            ;;
        rd.friday.retention.initrd_sha256=*)
            initrd_token=${token#rd.friday.retention.initrd_sha256=}
            initrd_count=$((initrd_count + 1))
            ;;
        rd.friday.retention*) fail ;;
        rdinit="$RDINIT") rdinit_count=$((rdinit_count + 1)) ;;
        rdinit=*) fail ;;
        retain_initrd) retain_count=$((retain_count + 1)) ;;
        sysctl.kernel.io_uring_disabled=2) io_uring_count=$((io_uring_count + 1)) ;;
        sysctl.kernel.io_uring_disabled=*) fail ;;
        noinitrd|init=*) fail ;;
        *) append_projected_token "$token" ;;
    esac
done
[ "$marker_count" -eq 1 ] && [ "$marker" = "$TRANSACTION" ] || fail
[ "$initrd_count" -eq 1 ] || fail
[ "$rdinit_count" -eq 1 ] || fail
[ "$retain_count" -eq 1 ] || fail
[ "$io_uring_count" -eq 1 ] || fail
hex64 "$initrd_token"
[ "$(digest_text "$PROJECTED_CMDLINE")" = "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256" ] || fail
[ "$(cat /proc/sys/kernel/io_uring_disabled)" = 2 ] || fail

BOOT_ID=$(cat /proc/sys/kernel/random/boot_id) || fail
[ "${#BOOT_ID}" -eq 36 ] || fail
case "$BOOT_ID" in *[!0-9a-f-]*) fail ;; esac
BOOT_ID_SHA256=$(digest_text "$BOOT_ID") || fail
CMDLINE_SHA256=$(digest_text "$CMDLINE") || fail
EXECUTING_INITRD_SHA256=$(digest /sys/firmware/initrd) || fail
[ "$EXECUTING_INITRD_SHA256" = "$initrd_token" ] || fail

EMBEDDED_IMAGE_AUTHORITY=$CONFIG/image-authority.v1.json
validate_root_file "$EMBEDDED_IMAGE_AUTHORITY" "$IMAGE_AUTHORITY_SHA256" 400
load_root_block_modules
resolve_root_device
ROOT_DEVICE_SHA256=$(digest_text "$ORDINARY_ROOT_DEVICE_ID") || fail

capture_premount_fixed_point

PRE_CORE=$(printf '{"boot_id_sha256":"%s","cmdline_sha256":"%s","executing_initrd_sha256":"%s","io_uring_disabled":2,"maintenance_cmdline_sha256":"%s","mount_namespace_sha256":"%s","mountinfo_sha256":"%s","namespace_epoch_sha256":"%s","nsfs_pins_absent":true,"only_pid1_userspace_task":true,"pid1_starttime_sha256":"%s","process_epoch_sha256":"%s","request_file_sha256":"%s","root_device_sha256":"%s","root_device_unmounted":true,"schema":"friday.release-artifact-maintenance-premount-receipt.v1","single_mount_namespace":true,"transaction_id":"%s"}' \
    "$BOOT_ID_SHA256" "$CMDLINE_SHA256" "$EXECUTING_INITRD_SHA256" \
    "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256" "$MOUNT_NAMESPACE_SHA256" \
    "$MOUNTINFO_SHA256" "$NAMESPACE_EPOCH_SHA256" \
    "$PID1_STARTTIME_SHA256" "$PROCESS_EPOCH_SHA256" "$REQUEST_FILE_SHA256" \
    "$ROOT_DEVICE_SHA256" "$TRANSACTION") || fail
PRE_DIGEST=$(digest_text "$PRE_CORE") || fail
PRE_TMP=$PREMOUNT.$$.new
printf '{"boot_id_sha256":"%s","cmdline_sha256":"%s","executing_initrd_sha256":"%s","io_uring_disabled":2,"maintenance_cmdline_sha256":"%s","mount_namespace_sha256":"%s","mountinfo_sha256":"%s","namespace_epoch_sha256":"%s","nsfs_pins_absent":true,"only_pid1_userspace_task":true,"pid1_starttime_sha256":"%s","process_epoch_sha256":"%s","receipt_sha256":"%s","request_file_sha256":"%s","root_device_sha256":"%s","root_device_unmounted":true,"schema":"friday.release-artifact-maintenance-premount-receipt.v1","single_mount_namespace":true,"transaction_id":"%s"}\n' \
    "$BOOT_ID_SHA256" "$CMDLINE_SHA256" "$EXECUTING_INITRD_SHA256" \
    "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256" "$MOUNT_NAMESPACE_SHA256" \
    "$MOUNTINFO_SHA256" "$NAMESPACE_EPOCH_SHA256" \
    "$PID1_STARTTIME_SHA256" "$PROCESS_EPOCH_SHA256" "$PRE_DIGEST" \
    "$REQUEST_FILE_SHA256" "$ROOT_DEVICE_SHA256" "$TRANSACTION" >"$PRE_TMP" || fail
chmod 0400 "$PRE_TMP" || fail
sync -f "$PRE_TMP" || fail
mv -f -- "$PRE_TMP" "$PREMOUNT" || fail
sync -f /run/friday-retention || fail
PREMOUNT_RECEIPT_SHA256=$(digest "$PREMOUNT") || fail

AUTH_CORE=$(printf '{"authority":"code_owned_first_pid1_premount_v1","boot_id_sha256":"%s","cmdline_sha256":"%s","executing_initrd_sha256":"%s","io_uring_disabled":2,"maintenance_cmdline_sha256":"%s","mount_namespace_sha256":"%s","namespace_epoch_sha256":"%s","ordinary_workloads_started":false,"premount_receipt_path":"/run/friday-retention/maintenance-premount-receipt.v1.json","premount_receipt_sha256":"%s","process_epoch_sha256":"%s","rdinit_path":"%s","request_file_sha256":"%s","request_path":"%s","request_sha256":"%s","root_device_sha256":"%s","root_device_unmounted":true,"schema":"friday.release-artifact-maintenance-premount-authority.v1","transaction_id":"%s"}' \
    "$BOOT_ID_SHA256" "$CMDLINE_SHA256" "$EXECUTING_INITRD_SHA256" \
    "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256" "$MOUNT_NAMESPACE_SHA256" \
    "$NAMESPACE_EPOCH_SHA256" "$PREMOUNT_RECEIPT_SHA256" \
    "$PROCESS_EPOCH_SHA256" "$RDINIT" "$REQUEST_FILE_SHA256" "$ROOT_REQUEST_PATH" \
    "$REQUEST_SHA256" "$ROOT_DEVICE_SHA256" "$TRANSACTION") || fail
AUTH_DIGEST=$(digest_text "$AUTH_CORE") || fail
AUTH_TMP=$AUTHORITY.$$.new
printf '{"authority":"code_owned_first_pid1_premount_v1","boot_id_sha256":"%s","cmdline_sha256":"%s","executing_initrd_sha256":"%s","io_uring_disabled":2,"maintenance_cmdline_sha256":"%s","mount_namespace_sha256":"%s","namespace_epoch_sha256":"%s","ordinary_workloads_started":false,"premount_receipt_path":"/run/friday-retention/maintenance-premount-receipt.v1.json","premount_receipt_sha256":"%s","process_epoch_sha256":"%s","rdinit_path":"%s","receipt_sha256":"%s","request_file_sha256":"%s","request_path":"%s","request_sha256":"%s","root_device_sha256":"%s","root_device_unmounted":true,"schema":"friday.release-artifact-maintenance-premount-authority.v1","transaction_id":"%s"}\n' \
    "$BOOT_ID_SHA256" "$CMDLINE_SHA256" "$EXECUTING_INITRD_SHA256" \
    "$MAINTENANCE_CMDLINE_TEMPLATE_SHA256" "$MOUNT_NAMESPACE_SHA256" \
    "$NAMESPACE_EPOCH_SHA256" "$PREMOUNT_RECEIPT_SHA256" \
    "$PROCESS_EPOCH_SHA256" "$RDINIT" "$AUTH_DIGEST" "$REQUEST_FILE_SHA256" \
    "$ROOT_REQUEST_PATH" "$REQUEST_SHA256" "$ROOT_DEVICE_SHA256" "$TRANSACTION" \
    >"$AUTH_TMP" || fail
chmod 0400 "$AUTH_TMP" || fail
sync -f "$AUTH_TMP" || fail
mv -f -- "$AUTH_TMP" "$AUTHORITY" || fail
sync -f /run/friday-retention || fail

mount -t ext4 -o rw "$ROOT_DEVICE_NODE" "$SYSROOT" || fail
mkdir -p "$SYSROOT/dev" "$SYSROOT/proc" "$SYSROOT/sys" "$SYSROOT/run" || fail
mount --rbind /dev "$SYSROOT/dev" || fail
mount --make-rslave "$SYSROOT/dev" || fail
mount --rbind /proc "$SYSROOT/proc" || fail
mount --make-rslave "$SYSROOT/proc" || fail
mount --rbind /sys "$SYSROOT/sys" || fail
mount --make-rslave "$SYSROOT/sys" || fail
mount --bind /run "$SYSROOT/run" || fail

validate_root_file "$SYSROOT$ROOT_REQUEST_PATH" "$REQUEST_FILE_SHA256" 444
validate_root_file "$SYSROOT$IMAGE_AUTHORITY_PATH" "$IMAGE_AUTHORITY_SHA256" 400
validate_root_file "$SYSROOT$CONTROLLER_PATH" "$CONTROLLER_SHA256" 555
OWNER_RECORD=$(chroot "$SYSROOT" /usr/bin/getent passwd "$OWNER_USER") || fail
[ "$(printf '%s\n' "$OWNER_RECORD" | wc -l)" -eq 1 ] || fail
[ "$(printf '%s\n' "$OWNER_RECORD" | cut -d: -f3)" -eq "$OWNER_UID" ] || fail

attempt=0
while ! chroot "$SYSROOT" /usr/sbin/runuser -u "$OWNER_USER" -- \
    /usr/bin/python3 -I -B -S "$CONTROLLER_PATH" execute \
    --request "$ROOT_REQUEST_PATH" \
    --expected-request-sha256 "$REQUEST_SHA256"
do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 8 ] || fail
    sleep 5
done

reboot_ordinary
