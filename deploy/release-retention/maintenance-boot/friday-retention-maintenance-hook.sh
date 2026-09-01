#!/bin/sh
set -eu

# The dedicated image is authorized only when the kernel executes the hashed
# launcher directly as rdinit/PID 1.  Reaching a dracut hook means rdinit was
# missing, malformed or bypassed; never continue into an ordinary boot path.
printf '%s\n' friday_retention_maintenance_rdinit_authority_missing >&2
while :; do
    poweroff -f 2>/dev/null || reboot -f 2>/dev/null || :
    sleep 1
done
