#!/bin/bash

check() {
    [[ ${FRIDAY_RETENTION_MAINTENANCE_BUILD:-0} == 1 ]] || return 255
    [[ -n ${FRIDAY_RETENTION_MAINTENANCE_CONFIG:-} ]] || return 1
    [[ -d ${FRIDAY_RETENTION_MAINTENANCE_CONFIG} ]] || return 1
    return 0
}

depends() {
    echo "base kernel-modules"
    return 0
}

maintenance_build_fail() {
    dfatal "friday retention maintenance image authority could not be installed"
    exit 1
}

read_config_line() {
    local path=$1
    local -a lines=()
    mapfile -t lines < "$path" || return 1
    [[ ${#lines[@]} -eq 1 && -n ${lines[0]} ]] || return 1
    CONFIG_VALUE=${lines[0]}
}

bind_root_block_authority() {
    local config=$1
    local reviewed_root_device_id root_device_id root_filesystem_uuid root_path root_type
    local sys_device device_id candidate_uuid probe_status device_count=0 match_count=0 matched_device_id=
    local block_inventory= block_inventory_after= inventory_count=0
    local scan_deadline=$((SECONDS + 120))
    local block_path block_path_after cursor module module_path module_chain=-
    local driver_seen=0
    local -a exact_modules=()
    local -A device_seen=() module_seen=()

    read_config_line "$config/ordinary-root-device-id" || return 1
    reviewed_root_device_id=$CONFIG_VALUE
    [[ $reviewed_root_device_id =~ ^[0-9]+:[0-9]+$ ]] || return 1
    read_config_line "$config/ordinary-root-filesystem-uuid" || return 1
    root_filesystem_uuid=$CONFIG_VALUE
    [[ $root_filesystem_uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || return 1

    # A UUID is usable as a cross-boot identity only when an uncached scan of
    # every current block device finds exactly one device.  The reviewed
    # dev_t is authenticated evidence about the review boot, not a durable
    # identity: dev_t may legitimately change across a power cycle.
    for sys_device in /sys/class/block/*; do
        ((SECONDS < scan_deadline)) || return 1
        [[ -d $sys_device && -f $sys_device/dev ]] || return 1
        device_count=$((device_count + 1))
        ((device_count <= 4096)) || return 1
        device_id=$(< "$sys_device/dev") || return 1
        [[ $device_id =~ ^[0-9]+:[0-9]+$ ]] || return 1
        [[ -z ${device_seen[$device_id]+present} ]] || return 1
        device_seen[$device_id]=1
        [[ -b /dev/block/$device_id ]] || return 1
        [[ $(get_maj_min "/dev/block/$device_id") == "$device_id" ]] || return 1
        block_inventory+="${sys_device##*/}:$device_id;"
        if candidate_uuid=$(timeout -s KILL 5 blkid -p -c /dev/null -s UUID -o value \
            "/dev/block/$device_id" 2> /dev/null); then
            probe_status=0
        else
            probe_status=$?
            candidate_uuid=
        fi
        [[ $probe_status == 0 || $probe_status == 2 ]] || return 1
        [[ $candidate_uuid != *$'\n'* ]] || return 1
        ((${#candidate_uuid} <= 64)) || return 1
        if [[ $candidate_uuid == "$root_filesystem_uuid" ]]; then
            match_count=$((match_count + 1))
            matched_device_id=$device_id
        fi
    done
    ((device_count > 0 && match_count == 1)) || return 1
    for sys_device in /sys/class/block/*; do
        ((SECONDS < scan_deadline)) || return 1
        [[ -d $sys_device && -f $sys_device/dev ]] || return 1
        inventory_count=$((inventory_count + 1))
        ((inventory_count <= 4096)) || return 1
        device_id=$(< "$sys_device/dev") || return 1
        [[ $device_id =~ ^[0-9]+:[0-9]+$ ]] || return 1
        block_inventory_after+="${sys_device##*/}:$device_id;"
    done
    ((inventory_count == device_count)) || return 1
    [[ $block_inventory_after == "$block_inventory" ]] || return 1
    root_device_id=$matched_device_id
    ((SECONDS < scan_deadline)) || return 1
    root_path=/dev/block/$root_device_id
    [[ -b $root_path ]] || return 1
    [[ $(get_maj_min "$root_path") == "$root_device_id" ]] || return 1
    [[ $(timeout -s KILL 5 blkid -p -c /dev/null -s UUID -o value "$root_path" 2> /dev/null) == "$root_filesystem_uuid" ]] \
        || return 1
    root_type=$(timeout -s KILL 5 blkid -p -c /dev/null -s TYPE -o value "$root_path" 2> /dev/null) \
        || return 1
    [[ $root_type == ext4 ]] || return 1
    ((SECONDS < scan_deadline)) || return 1

    # This direct-rdinit image deliberately supports a physical root block
    # device (or partition), not an assembly that would require LVM/MD/crypt
    # userspace before the global premount proof.
    for sys_device in "/sys/dev/block/$root_device_id"/slaves/*; do
        [[ ! -e $sys_device ]] || return 1
    done
    block_path=$(readlink -f "/sys/dev/block/$root_device_id") || return 1
    [[ $block_path == /sys/devices/* ]] || return 1
    cursor=$block_path
    while [[ $cursor != /sys/devices && $cursor != / ]]; do
        if [[ -L $cursor/driver ]]; then
            driver_seen=1
        fi
        if [[ -L $cursor/driver/module ]]; then
            module_path=$(readlink -f "$cursor/driver/module") || return 1
            module=${module_path##*/}
            [[ $module =~ ^[A-Za-z0-9_-]+$ ]] || return 1
            if [[ -z ${module_seen[$module]+present} ]]; then
                module_seen[$module]=1
                exact_modules+=("$module")
            fi
        fi
        cursor=${cursor%/*}
        [[ -n $cursor ]] || cursor=/
    done
    ((driver_seen == 1)) || return 1
    block_path_after=$(readlink -f "/sys/dev/block/$root_device_id") || return 1
    [[ $block_path_after == "$block_path" ]] || return 1

    if ((${#exact_modules[@]} > 0)); then
        printf -v module_chain '%s:' "${exact_modules[@]}"
        module_chain=${module_chain%:}
        hostonly='' instmods "${exact_modules[@]}" || return 1
    fi
    hostonly='' instmods -o ext4 || return 1
    ROOT_BLOCK_MODULE_CHAIN=$module_chain
}

install() {
    local config=${FRIDAY_RETENTION_MAINTENANCE_CONFIG}
    local launcher=/usr/libexec/friday/release_artifact_retention_maintenance_launcher
    local runner=/usr/libexec/friday/release_artifact_retention_maintenance_runner.sh
    local hook=/usr/libexec/friday/release_artifact_retention_maintenance_hook.sh
    local name
    [[ -x $launcher && -x $runner && -x $hook ]] || maintenance_build_fail
    for name in \
        transaction-id request-path root-request-path request-file-sha256 \
        request-sha256 image-authority-path image-authority-sha256 \
        controller-path controller-sha256 owner-user owner-uid \
        maintenance-cmdline-sha256 ordinary-root-device-id \
        ordinary-root-filesystem-uuid \
        image-authority.v1.json
    do
        [[ -f $config/$name && ! -L $config/$name ]] || maintenance_build_fail
    done

    # The proof relies on rdinit being the first PID in the initial mount
    # namespace, not on manufacturing a private view.
    bind_root_block_authority "$config" || maintenance_build_fail
    inst_multiple \
        blkid cat chmod chroot cut date id mkdir mknod modprobe mount mv \
        poweroff readlink reboot rm sha256sum sleep stat sync timeout tr \
        umount wc \
        || maintenance_build_fail
    inst_dir \
        /dev /etc/friday-retention-maintenance /proc \
        /run /run/friday-retention /sys /sysroot /usr/libexec/friday \
        || maintenance_build_fail
    inst_simple "$launcher" "$launcher" || maintenance_build_fail
    inst_script "$runner" "$runner" || maintenance_build_fail
    inst_script "$hook" "$hook" || maintenance_build_fail
    inst_hook pre-mount 00 "$hook" || maintenance_build_fail
    printf '%s\n' "$ROOT_BLOCK_MODULE_CHAIN" \
        > "$initdir/etc/friday-retention-maintenance/root-block-module-chain" \
        || maintenance_build_fail
    chmod 0400 "$initdir/etc/friday-retention-maintenance/root-block-module-chain" \
        || maintenance_build_fail
    for name in \
        transaction-id request-path root-request-path request-file-sha256 \
        request-sha256 image-authority-path image-authority-sha256 \
        controller-path controller-sha256 owner-user owner-uid \
        maintenance-cmdline-sha256 ordinary-root-device-id \
        ordinary-root-filesystem-uuid \
        image-authority.v1.json
    do
        inst_simple "$config/$name" "/etc/friday-retention-maintenance/$name" \
            || maintenance_build_fail
    done
}
