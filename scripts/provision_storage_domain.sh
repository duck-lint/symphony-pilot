#!/bin/sh
# One-time root provisioning for the fixed Symphony ext4 project-quota pool.
#
# This is not called by Pilot, Runtime, the adapter, or a task. It accepts one
# operator-selected dedicated block device, refuses /dev/sdd and automatic
# growth, and installs only the fixed capability-specific helper. Normal task
# execution remains unprivileged.
set -eu
umask 077
# Root provisioning is a fixed operator action. Do not inherit compiler,
# shell, Python, Git, or PATH resolution controls from the caller.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset CDPATH ENV BASH_ENV PYTHONPATH PYTHONHOME CC CFLAGS CPPFLAGS LDFLAGS
CC=/usr/bin/cc

POOL_DEVICE=${1:?usage: provision_storage_domain.sh /dev/<dedicated-device>}
SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
POOL_ROOT=/home/duck-lint/symphony-workspaces
HELPER_SOURCE=$SCRIPT_ROOT/provisioning/quota-admit-task.c
SUPERVISOR_SOURCE=$SCRIPT_ROOT/runtime/wsl_contained_exec.py
VHD_OPERATOR_SOURCE=$SCRIPT_ROOT/scripts/provision_storage_vhdx.ps1
HELPER=/var/lib/symphony-pilot/quota-admit-task
HELPER_GROUP=symphony-pilot
IDENTITY=/var/lib/symphony-pilot/quota-admit-task.identity.json
STORAGE_IDENTITY=/var/lib/symphony-pilot/storage-domain.identity.json
FSTAB=/etc/fstab
POOL_LABEL=SYMPHONY-POOL
EXPECTED_BYTES=$((64 * 1024 * 1024 * 1024))
ALLOCATABLE_BYTES=$((63 * 1024 * 1024 * 1024))
MIN_INODES=500000
FSTAB_TMP=
HELPER_TMP=
IDENTITY_TMP=
STORAGE_IDENTITY_TMP=

cleanup() {
    [ -z "$FSTAB_TMP" ] || rm -f -- "$FSTAB_TMP"
    [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"
    [ -z "$IDENTITY_TMP" ] || rm -f -- "$IDENTITY_TMP"
    [ -z "$STORAGE_IDENTITY_TMP" ] || rm -f -- "$STORAGE_IDENTITY_TMP"
}
trap cleanup EXIT HUP INT TERM

fail() {
    echo "symphony storage provisioning stopped: $*" >&2
    exit 78
}

[ "$(id -u)" -eq 0 ] || fail "must run once as root"
[ "$(id -u duck-lint)" -ge 0 ] || fail "fixed duck-lint account is required"

# This script is executable only from an atomic, manifest-covered deployment.
# The deployed supervisor verifies the script, helper source, VHDX contract,
# and all other authority files before any device or filesystem operation.
[ -f "$SUPERVISOR_SOURCE" ] || fail "deployed supervisor is unavailable"
[ -f "$HELPER_SOURCE" ] || fail "deployed quota helper source is unavailable"
[ -f "$VHD_OPERATOR_SOURCE" ] || fail "deployed fixed-VHDX contract is unavailable"
/usr/bin/python3 -B "$SUPERVISOR_SOURCE" --verify-deployment >/dev/null || \
    fail "the provisioning authority is not the exact deployed Pilot snapshot"
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

compile_helper_preflight() {
    [ -f "$CC" ] && [ -x "$CC" ] || \
        fail "trusted provisioning prerequisite /usr/bin/cc is unavailable"
    HELPER_TMP=$(mktemp /tmp/.symphony-pilot-quota-admit-task.XXXXXX) || \
        fail "trusted provisioning prerequisite helper temporary file is unavailable"
    chown root:root "$HELPER_TMP" || \
        fail "trusted provisioning prerequisite helper temporary file ownership is unsafe"
    chmod 0700 "$HELPER_TMP" || \
        fail "trusted provisioning prerequisite helper temporary file mode is unsafe"
    if ! /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        "$CC" -std=c11 -O2 -Wall -Wextra -Werror "$HELPER_SOURCE" \
        -o "$HELPER_TMP" >/dev/null 2>&1; then
        fail "trusted provisioning prerequisite helper compilation failed"
    fi
    chown root:root "$HELPER_TMP" || \
        fail "trusted provisioning prerequisite helper output ownership is unsafe"
    chmod 0700 "$HELPER_TMP" || \
        fail "trusted provisioning prerequisite helper output mode is unsafe"
    [ -f "$HELPER_TMP" ] || fail "trusted provisioning prerequisite helper output is unavailable"
    [ "$(stat -c '%u %a' "$HELPER_TMP")" = "0 700" ] || \
        fail "trusted provisioning prerequisite helper output privilege state is unsafe"
    COMPILED_HELPER_SHA256=$(sha256sum "$HELPER_TMP" | awk '{print $1}')
    [ "${COMPILED_HELPER_SHA256:-}" ] || \
        fail "trusted provisioning prerequisite helper output identity is unavailable"
}

compile_helper_preflight

DEVICE_TYPE=$(blkid -o value -s TYPE "$POOL_DEVICE" 2>/dev/null || true)
POOL_DEVICE_REAL=$(readlink -f -- "$POOL_DEVICE")
if [ -n "$DEVICE_TYPE" ]; then
    [ "$DEVICE_TYPE" = "ext4" ] || fail "existing device filesystem is not the Symphony ext4 domain"
    [ "$(blkid -o value -s LABEL "$POOL_DEVICE")" = "$POOL_LABEL" ] || \
        fail "existing filesystem is not the identified Symphony pool"
fi

verify_mount() {
    [ "$(findmnt -no TARGET --target "$POOL_ROOT")" = "$POOL_ROOT" ] || fail "pool mount target is wrong"
    MOUNT_SOURCE=$(findmnt -no SOURCE --target "$POOL_ROOT")
    [ -n "$MOUNT_SOURCE" ] || fail "pool mount source is unavailable"
    MOUNT_SOURCE_REAL=$(readlink -f -- "$MOUNT_SOURCE") || fail "pool mount source cannot be resolved"
    [ "$MOUNT_SOURCE_REAL" = "$POOL_DEVICE_REAL" ] || fail "pool mount source differs from the dedicated device"
    [ "$(findmnt -no FSTYPE --target "$POOL_ROOT")" = "ext4" ] || fail "pool filesystem is not ext4"
    MOUNT_OPTIONS=$(findmnt -no OPTIONS --target "$POOL_ROOT")
    case ",$MOUNT_OPTIONS," in *,prjquota,*|*,pquota,*) ;; *) fail "pool is not mounted with project quota enforcement" ;; esac
    [ "$(findmnt -no UUID --target "$POOL_ROOT")" = "$POOL_UUID" ] || fail "pool mount UUID differs from the dedicated device"
    [ "$(blockdev --getsize64 "$POOL_DEVICE")" -eq "$EXPECTED_BYTES" ] || \
        fail "mounted pool backing device is not exactly 64 GiB"
}

verify_device_filesystem() {
    DEVICE_TYPE=$(blkid -o value -s TYPE "$POOL_DEVICE")
    DEVICE_LABEL=$(blkid -o value -s LABEL "$POOL_DEVICE")
    [ "$DEVICE_TYPE" = "ext4" ] || fail "Symphony pool filesystem type is not ext4"
    [ "$DEVICE_LABEL" = "$POOL_LABEL" ] || fail "Symphony pool label is not exact"
    FEATURES=$(tune2fs -l "$POOL_DEVICE" | sed -n 's/^Filesystem features:[[:space:]]*//p')
    case " $FEATURES " in *" project "*) ;; *) fail "formatted filesystem lacks ext4 project support" ;; esac
    case " $FEATURES " in *" quota "*) ;; *) fail "formatted filesystem lacks ext4 quota storage" ;; esac
    PROJECT_QUOTA_INODE=$(tune2fs -l "$POOL_DEVICE" | awk -F: '$1 == "Project quota inode" {gsub(/[[:space:]]/, "", $2); print $2}')
    [ "${PROJECT_QUOTA_INODE:-0}" -gt 0 ] || fail "project quota inode was not initialized"
    [ "$(tune2fs -l "$POOL_DEVICE" | awk -F: '$1 == "Reserved block count" {gsub(/[[:space:]]/, "", $2); print $2}')" = "0" ] || \
        fail "reserved ext4 blocks are not zero"
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

[ -n "$DEVICE_TYPE" ] && POOL_UUID=$(blkid -s UUID -o value "$POOL_DEVICE")
if mountpoint -q "$POOL_ROOT"; then
    # A mounted target is evidence, not an invitation to mutate.  In
    # particular, an unrelated UUID must fail before chown/chmod/fstab/helper
    # changes can occur.
    [ -n "$DEVICE_TYPE" ] || fail "mounted pool target has no dedicated filesystem identity"
    [ -n "${POOL_UUID:-}" ] || fail "dedicated filesystem UUID is unavailable"
    verify_device_filesystem
    verify_mount
    POOL_MOUNTED=1
else
    [ ! -L "$POOL_ROOT" ] || fail "pool mount target is a symlink"
    mkdir -p "$POOL_ROOT"
    if [ -z "$DEVICE_TYPE" ]; then
        # project supplies FS_IOC_FS{GET,SET}XATTR project IDs; quota supplies hidden
        # ext4 quota inodes; quotatype initializes the project quota inode. Zero
        # reserved blocks is intentional for this dedicated task-only filesystem:
        # Pilot's eight-GiB emergency reserve is a separate admission policy.
        mkfs.ext4 -L "$POOL_LABEL" -m 0 -i 65536 -I 256 -J size=64 \
        -O project,quota -E quotatype=prjquota "$POOL_DEVICE"
        POOL_UUID=$(blkid -s UUID -o value "$POOL_DEVICE")
    fi
    verify_device_filesystem
    [ -n "${POOL_UUID:-}" ] || fail "dedicated filesystem UUID is unavailable"
    mount -t ext4 -o prjquota "$POOL_DEVICE" "$POOL_ROOT"
    POOL_MOUNTED=1
    verify_mount
fi

# The deployed verifier requires the shared pool root to be owned by the
# unprivileged execution account and non-writable by group/other. Establish
# that trust boundary on the mounted filesystem, not its pre-mount directory.
chown duck-lint:duck-lint "$POOL_ROOT"
chmod 0750 "$POOL_ROOT"

write_storage_identity() {
    STORAGE_IDENTITY_TMP=$(mktemp /var/.symphony-pilot-storage-identity.XXXXXX)
    printf '{"schema":"symphony-pilot-storage-domain/v1","pool_label":"%s","filesystem_uuid":"%s","backing_bytes":%s,"allocatable_bytes":%s,"filesystem":"ext4","mount_target":"%s","quota_features":["project","quota"],"mount_options":["prjquota"],"reserved_blocks":0}\n' \
        "$POOL_LABEL" "$POOL_UUID" "$EXPECTED_BYTES" "$ALLOCATABLE_BYTES" "$POOL_ROOT" \
        > "$STORAGE_IDENTITY_TMP"
    install -o root -g "$HELPER_GROUP" -m 0640 "$STORAGE_IDENTITY_TMP" "$STORAGE_IDENTITY"
    rm -f -- "$STORAGE_IDENTITY_TMP"
    STORAGE_IDENTITY_TMP=
}

verify_storage_identity() {
    [ -f "$STORAGE_IDENTITY" ] && [ ! -L "$STORAGE_IDENTITY" ] || \
        fail "storage-domain identity record is unavailable"
    [ "$(stat -c '%u %g %a' "$STORAGE_IDENTITY")" = "0 $EXPECTED_GID 640" ] || \
        fail "storage-domain identity record has unsafe privilege state"
    grep -Fq '"schema":"symphony-pilot-storage-domain/v1"' "$STORAGE_IDENTITY" || fail "storage-domain identity schema is wrong"
    grep -Fq '"pool_label":"SYMPHONY-POOL"' "$STORAGE_IDENTITY" || fail "storage-domain identity label is wrong"
    grep -Fq '"filesystem":"ext4"' "$STORAGE_IDENTITY" || fail "storage-domain identity filesystem is wrong"
    grep -Fq '"mount_target":"/home/duck-lint/symphony-workspaces"' "$STORAGE_IDENTITY" || fail "storage-domain identity target is wrong"
    grep -Fq '"quota_features":["project","quota"]' "$STORAGE_IDENTITY" || fail "storage-domain quota features are wrong"
    grep -Fq '"mount_options":["prjquota"]' "$STORAGE_IDENTITY" || fail "storage-domain mount options are wrong"
    grep -Fq '"reserved_blocks":0' "$STORAGE_IDENTITY" || fail "storage-domain reserved blocks are wrong"
    grep -Fq '"filesystem_uuid":"'"$POOL_UUID"'"' "$STORAGE_IDENTITY" || fail "storage-domain UUID differs from the device"
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
if ! cmp -s "$FSTAB_TMP" "$FSTAB"; then
    install -o root -g root -m 0644 "$FSTAB_TMP" "$FSTAB"
fi
rm -f -- "$FSTAB_TMP"
FSTAB_TMP=

getent group "$HELPER_GROUP" >/dev/null 2>&1 || groupadd --system "$HELPER_GROUP"
EXPECTED_GID=$(getent group "$HELPER_GROUP" | awk -F: '{print $3}')
install -d -o root -g "$HELPER_GROUP" -m 0750 /var/lib/symphony-pilot
if [ -e "$STORAGE_IDENTITY" ]; then
    verify_storage_identity
else
    write_storage_identity
fi

# Re-read the exact persistent entry through the normal mount path, then prove
# the quota state survives the remount before any task admission is possible.
umount "$POOL_ROOT"
mount "$POOL_ROOT"
verify_mount
verify_capacity
verify_storage_identity

usermod --append --groups "$HELPER_GROUP" duck-lint
install_verified_helper() {
    if [ -e "$HELPER" ]; then
        [ ! -L "$HELPER" ] || fail "existing quota helper is a symlink"
        [ "$(stat -c '%u %g %a' "$HELPER")" = "0 $EXPECTED_GID 4750" ] || \
            fail "existing quota helper privilege state conflicts"
    else
        install -o root -g "$HELPER_GROUP" -m 4750 "$HELPER_TMP" "$HELPER"
    fi
    HELPER_SHA256=$(sha256sum "$HELPER" | awk '{print $1}')
    [ "$HELPER_SHA256" = "$COMPILED_HELPER_SHA256" ] || \
        fail "installed quota helper bytes differ from the preflight build"
    cmp -s "$HELPER_TMP" "$HELPER" || \
        fail "installed quota helper bytes differ from the preflight object"
    rm -f -- "$HELPER_TMP"
    HELPER_TMP=
}

install_verified_helper

HELPER_UID=$(stat -c '%u' "$HELPER")
HELPER_GID=$(stat -c '%g' "$HELPER")
EXPECTED_GID=$(getent group "$HELPER_GROUP" | awk -F: '{print $3}')
[ "$HELPER_UID" = "0" ] || fail "quota helper is not root-owned"
[ "$HELPER_GID" = "$EXPECTED_GID" ] || fail "quota helper group is not the reviewed group"
[ "$(stat -c '%a' "$HELPER")" = "4750" ] || fail "quota helper is not exactly setuid-root mode 4750"

if [ -e "$IDENTITY" ]; then
    [ ! -L "$IDENTITY" ] || fail "existing quota helper identity is a symlink"
    [ "$(stat -c '%u %g %a' "$IDENTITY")" = "0 $EXPECTED_GID 640" ] || \
        fail "existing quota helper identity privilege state conflicts"
    grep -Fq '"schema":"symphony-pilot-quota-helper/v1"' "$IDENTITY" || fail "existing quota helper identity schema conflicts"
    grep -Fq '"source_sha256":"'"$ACTUAL_SOURCE_SHA256"'"' "$IDENTITY" || fail "existing quota helper source identity conflicts"
    grep -Fq '"helper_sha256":"'"$HELPER_SHA256"'"' "$IDENTITY" || fail "existing quota helper binary identity conflicts"
    grep -Fq '"group":"'"$HELPER_GROUP"'"' "$IDENTITY" || fail "existing quota helper group identity conflicts"
    grep -Fq '"privilege":"setuid-root"' "$IDENTITY" || fail "existing quota helper privilege identity conflicts"
else
    IDENTITY_TMP=/var/lib/symphony-pilot/quota-admit-task.identity.json.tmp
    printf '{"schema":"symphony-pilot-quota-helper/v1","source_sha256":"%s","helper_sha256":"%s","group":"%s","privilege":"setuid-root"}\n' \
        "$ACTUAL_SOURCE_SHA256" "$HELPER_SHA256" "$HELPER_GROUP" > "$IDENTITY_TMP"
    install -o root -g "$HELPER_GROUP" -m 0640 "$IDENTITY_TMP" "$IDENTITY"
    rm -f -- "$IDENTITY_TMP"
    IDENTITY_TMP=
fi
HELPER_TMP=

# This fixed helper operation performs generic PRJQUOTA Q_GETQUOTA and
# Q_SETQUOTA against the pool before any task-shaped admission is possible.
"$HELPER" --operation verify-pool >/dev/null || fail "generic project quota get/set verification failed"

echo "provisioned fixed Symphony ext4 project-quota pool; run the trusted Pilot verifier before admission"
