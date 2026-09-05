#!/bin/sh
# One-time root provisioning for the fixed Symphony ext4 project-quota pool.
#
# This is not called by Pilot, Runtime, the adapter, or a task. It accepts one
# operator-selected dedicated block device, refuses /dev/sdd and automatic
# growth, and installs only the fixed capability-specific helper. Normal task
# execution remains unprivileged.
set -eu
umask 077

POOL_DEVICE=${1:?usage: provision_storage_domain.sh /dev/<dedicated-device>}
SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
POOL_ROOT=/home/duck-lint/symphony-workspaces
HELPER_SOURCE=$SCRIPT_ROOT/provisioning/quota-admit-task.c
SUPERVISOR_SOURCE=$SCRIPT_ROOT/runtime/wsl_contained_exec.py
HELPER=/usr/libexec/symphony-pilot/quota-admit-task
HELPER_GROUP=symphony-pilot
IDENTITY=/etc/symphony-pilot/quota-admit-task.identity.json
FSTAB=/etc/fstab
EXPECTED_BYTES=$((64 * 1024 * 1024 * 1024))
ALLOCATABLE_BYTES=$((63 * 1024 * 1024 * 1024))
MIN_INODES=500000
FSTAB_TMP=
HELPER_TMP=
IDENTITY_TMP=

cleanup() {
    [ -z "$FSTAB_TMP" ] || rm -f -- "$FSTAB_TMP"
    [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"
    [ -z "$IDENTITY_TMP" ] || rm -f -- "$IDENTITY_TMP"
}
trap cleanup EXIT HUP INT TERM

fail() {
    echo "symphony storage provisioning stopped: $*" >&2
    exit 78
}

[ "$(id -u)" -eq 0 ] || fail "must run once as root"
[ "$(id -u duck-lint)" -ge 0 ] || fail "fixed duck-lint account is required"
case "$POOL_DEVICE" in
    /dev/*) ;;
    *) fail "dedicated block device must be under /dev" ;;
esac
case "$POOL_DEVICE" in
    /dev/sdd|/dev/sdd/*) fail "ordinary Ubuntu root device is rejected" ;;
esac
[ -b "$POOL_DEVICE" ] || fail "dedicated block device is required"
[ "$(blockdev --getsize64 "$POOL_DEVICE")" -eq "$EXPECTED_BYTES" ] || \
    fail "backing device must be exactly 64 GiB; automatic expansion is disabled"

[ -f "$FSTAB" ] && [ ! -L "$FSTAB" ] || fail "/etc/fstab must be a real file"
FSTAB_METADATA=$(stat -c '%u %a' "$FSTAB")
[ "$FSTAB_METADATA" = "0 644" ] || fail "/etc/fstab must be root-owned mode 0644"

# The reviewed supervisor is the source of the expected digest. The actual C
# bytes are hashed before the compiler is invoked, so a stale sidecar value
# cannot bless changed helper input.
EXPECTED_SOURCE_SHA256=$(sed -n '/QUOTA_HELPER_SOURCE_SHA256 = (/{n;s/[^"a-f0-9]*"\([a-f0-9]\{64\}\)".*/\1/p;}' "$SUPERVISOR_SOURCE")
[ "${EXPECTED_SOURCE_SHA256:-}" ] || fail "reviewed helper source digest is unavailable"
ACTUAL_SOURCE_SHA256=$(sha256sum "$HELPER_SOURCE" | awk '{print $1}')
[ "$ACTUAL_SOURCE_SHA256" = "$EXPECTED_SOURCE_SHA256" ] || \
    fail "quota helper source differs from the reviewed supervisor digest"

if blkid -o value -s TYPE "$POOL_DEVICE" >/dev/null 2>&1; then
    fail "refusing to overwrite a device with an existing filesystem"
fi
mountpoint -q "$POOL_ROOT" && fail "storage pool mount target is already occupied"

# project supplies FS_IOC_FS{GET,SET}XATTR project IDs; quota supplies hidden
# ext4 quota inodes; quotatype initializes the project quota inode. Zero
# reserved blocks is intentional for this dedicated task-only filesystem:
# Pilot's eight-GiB emergency reserve is a separate admission policy.
mkfs.ext4 -m 0 -i 65536 -I 256 -J size=64 \
    -O project,quota -E quotatype=prjquota "$POOL_DEVICE"
FEATURES=$(tune2fs -l "$POOL_DEVICE" | sed -n 's/^Filesystem features:[[:space:]]*//p')
case " $FEATURES " in *" project "*) ;; *) fail "formatted filesystem lacks ext4 project support" ;; esac
case " $FEATURES " in *" quota "*) ;; *) fail "formatted filesystem lacks ext4 quota storage" ;; esac
PROJECT_QUOTA_INODE=$(tune2fs -l "$POOL_DEVICE" | awk -F: '$1 == "Project quota inode" {gsub(/[[:space:]]/, "", $2); print $2}')
[ "${PROJECT_QUOTA_INODE:-0}" -gt 0 ] || fail "project quota inode was not initialized"
[ "$(tune2fs -l "$POOL_DEVICE" | awk -F: '$1 == "Reserved block count" {gsub(/[[:space:]]/, "", $2); print $2}')" = "0" ] || \
    fail "reserved ext4 blocks are not zero"

mkdir -p "$POOL_ROOT"
mount -t ext4 -o prjquota "$POOL_DEVICE" "$POOL_ROOT"
# The deployed verifier requires the shared pool root to be owned by the
# unprivileged execution account and non-writable by group/other. Establish
# that trust boundary on the mounted filesystem, not its pre-mount directory.
chown duck-lint:duck-lint "$POOL_ROOT"
chmod 0750 "$POOL_ROOT"

verify_mount() {
    [ "$(findmnt -no TARGET --target "$POOL_ROOT")" = "$POOL_ROOT" ] || fail "pool mount target is wrong"
    [ "$(findmnt -no FSTYPE --target "$POOL_ROOT")" = "ext4" ] || fail "pool filesystem is not ext4"
    MOUNT_OPTIONS=$(findmnt -no OPTIONS --target "$POOL_ROOT")
    case ",$MOUNT_OPTIONS," in *,prjquota,*|*,pquota,*) ;; *) fail "pool is not mounted with project quota enforcement" ;; esac
    [ "$(findmnt -no UUID --target "$POOL_ROOT")" = "$POOL_UUID" ] || fail "pool mount UUID differs from the dedicated device"
}

verify_capacity() {
    read -r BLOCK_SIZE TOTAL_BLOCKS AVAILABLE_BLOCKS TOTAL_INODES AVAILABLE_INODES <<EOF
$(stat -f -c '%S %b %a %c %d' "$POOL_ROOT")
EOF
    [ $((BLOCK_SIZE * TOTAL_BLOCKS)) -le "$EXPECTED_BYTES" ] || fail "filesystem exceeds fixed backing capacity"
    [ $((BLOCK_SIZE * AVAILABLE_BLOCKS)) -ge "$ALLOCATABLE_BYTES" ] || \
        fail "unprivileged f_bavail capacity is below 63 GiB"
    [ "$AVAILABLE_INODES" -ge "$MIN_INODES" ] || fail "unprivileged f_favail inode headroom is insufficient"
}

POOL_UUID=$(blkid -s UUID -o value "$POOL_DEVICE")
verify_mount
verify_capacity

# Write one exact managed entry to /etc/fstab. A different or duplicate entry
# for this mount target is a configuration conflict; unrelated entries are
# copied byte-for-byte and are never replaced by a generated fragment.
FSTAB_ENTRY="UUID=$POOL_UUID $POOL_ROOT ext4 nofail,prjquota 0 2"
FSTAB_TMP=$(mktemp /etc/.symphony-pilot-fstab.XXXXXX)
if ! awk -v root="$POOL_ROOT" -v desired="$FSTAB_ENTRY" '
    BEGIN { found = 0; conflict = 0 }
    /^[[:space:]]*#/ { print; next }
    NF >= 2 && $2 == root {
        found++
        if ($0 != desired) conflict = 1
    }
    { print }
    END {
        if (conflict || found > 1) exit 42
        if (found == 0) print desired
    }
' "$FSTAB" > "$FSTAB_TMP"; then
    fail "/etc/fstab already contains a conflicting Symphony mount entry"
fi
install -o root -g root -m 0644 "$FSTAB_TMP" "$FSTAB"
rm -f -- "$FSTAB_TMP"
FSTAB_TMP=

# Re-read the exact persistent entry through the normal mount path, then prove
# the quota state survives the remount before any task admission is possible.
umount "$POOL_ROOT"
mount "$POOL_ROOT"
verify_mount
verify_capacity

getent group "$HELPER_GROUP" >/dev/null 2>&1 || groupadd --system "$HELPER_GROUP"
usermod --append --groups "$HELPER_GROUP" duck-lint
install -d -o root -g root -m 0755 /etc/symphony-pilot
install -d -o root -g root -m 0755 /usr/libexec/symphony-pilot
HELPER_TMP=/etc/symphony-pilot/quota-admit-task.tmp
cc -std=c11 -O2 -Wall -Wextra -Werror "$HELPER_SOURCE" -o "$HELPER_TMP"
install -o root -g "$HELPER_GROUP" -m 4750 "$HELPER_TMP" "$HELPER"

HELPER_UID=$(stat -c '%u' "$HELPER")
HELPER_GID=$(stat -c '%g' "$HELPER")
EXPECTED_GID=$(getent group "$HELPER_GROUP" | awk -F: '{print $3}')
[ "$HELPER_UID" = "0" ] || fail "quota helper is not root-owned"
[ "$HELPER_GID" = "$EXPECTED_GID" ] || fail "quota helper group is not the reviewed group"
[ "$(stat -c '%a' "$HELPER")" = "4750" ] || fail "quota helper is not exactly setuid-root mode 4750"

HELPER_SHA256=$(sha256sum "$HELPER" | awk '{print $1}')
IDENTITY_TMP=/etc/symphony-pilot/quota-admit-task.identity.json.tmp
printf '{"schema":"symphony-pilot-quota-helper/v1","source_sha256":"%s","helper_sha256":"%s","group":"%s","privilege":"setuid-root"}\n' \
    "$ACTUAL_SOURCE_SHA256" "$HELPER_SHA256" "$HELPER_GROUP" > "$IDENTITY_TMP"
install -o root -g root -m 0644 "$IDENTITY_TMP" "$IDENTITY"
rm -f -- "$IDENTITY_TMP" "$HELPER_TMP"
IDENTITY_TMP=
HELPER_TMP=

# This fixed helper operation performs generic PRJQUOTA Q_GETQUOTA and
# Q_SETQUOTA against the pool before any task-shaped admission is possible.
"$HELPER" --operation verify-pool >/dev/null || fail "generic project quota get/set verification failed"

echo "provisioned fixed Symphony ext4 project-quota pool; run the trusted Pilot verifier before admission"
