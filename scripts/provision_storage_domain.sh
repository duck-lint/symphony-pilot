#!/bin/sh
# One-time operator provisioning for the fixed Symphony storage domain.
#
# This is intentionally not called by Pilot, Runtime, the adapter, or a task.
# It is a reviewable root-only installation recipe.  The device must already
# be the operator's dedicated fixed-size VHDX/block attachment; no device
# discovery, fallback, growth, or generic sudo surface is provided here.
set -eu

POOL_DEVICE=${1:?usage: provision_storage_domain.sh /dev/<dedicated-device>}
POOL_ROOT=/home/duck-lint/symphony-workspaces
HELPER=/usr/libexec/symphony-pilot/quota-admit-task
HELPER_GROUP=symphony-pilot
EXPECTED_BYTES=$((64 * 1024 * 1024 * 1024))

[ "$(id -u)" -eq 0 ] || { echo "must run once as root" >&2; exit 78; }
[ "$(id -u duck-lint)" -ge 0 ] || { echo "fixed duck-lint account is required" >&2; exit 78; }
getent group "$HELPER_GROUP" >/dev/null 2>&1 || groupadd --system "$HELPER_GROUP"
usermod --append --groups "$HELPER_GROUP" duck-lint
case "$POOL_DEVICE" in /dev/sdd|/dev/sdd/*) echo "ordinary Ubuntu root device is rejected" >&2; exit 78;; esac
[ -b "$POOL_DEVICE" ] || { echo "dedicated block device is required" >&2; exit 78; }
[ "$(blockdev --getsize64 "$POOL_DEVICE")" -eq "$EXPECTED_BYTES" ] || {
    echo "backing device must be exactly 64 GiB; automatic expansion is disabled" >&2; exit 78;
}

# The operator must confirm that this is a newly dedicated device.  Refuse an
# existing filesystem rather than destroying unknown data.
if blkid -o value -s TYPE "$POOL_DEVICE" >/dev/null 2>&1; then
    echo "refusing to overwrite a device with an existing filesystem" >&2
    exit 78
fi
mkfs.ext4 -O project "$POOL_DEVICE"
mkdir -p "$POOL_ROOT"
mount -o prjquota "$POOL_DEVICE" "$POOL_ROOT"
findmnt --target "$POOL_ROOT" --output TARGET,SOURCE,FSTYPE,OPTIONS

install -d -o root -g root -m 0755 /etc/symphony-pilot
install -d -o root -g root -m 0755 /etc/fstab.d
install -d -o root -g root -m 0755 /usr/libexec/symphony-pilot
cc -O2 -Wall -Wextra -Werror provisioning/quota-admit-task.c \
    -o /etc/symphony-pilot/quota-admit-task.tmp
install -o root -g "$HELPER_GROUP" -m 4750 \
    /etc/symphony-pilot/quota-admit-task.tmp "$HELPER"
sha256sum "$HELPER" | awk '{print $1}' > /etc/symphony-pilot/quota-admit-task.sha256.tmp
cat > /etc/symphony-pilot/quota-admit-task.identity.json.tmp <<EOF
{"schema":"symphony-pilot-quota-helper/v1","source_sha256":"8824ee3621b109950927de1b54a83c468aa1db28515164a8a6ce9090a528b0a5","helper_sha256":"$(cat /etc/symphony-pilot/quota-admit-task.sha256.tmp)","privilege":"setuid-root"}
EOF
install -o root -g root -m 0644 /etc/symphony-pilot/quota-admit-task.identity.json.tmp \
    /etc/symphony-pilot/quota-admit-task.identity.json
rm -f /etc/symphony-pilot/quota-admit-task.sha256.tmp \
    /etc/symphony-pilot/quota-admit-task.identity.json.tmp \
    /etc/symphony-pilot/quota-admit-task.tmp

# Persist only the fixed mount identity.  The operator supplies the device
# once; WSL restart handling must preserve this exact UUID, never resize it.
UUID=$(blkid -s UUID -o value "$POOL_DEVICE")
printf 'UUID=%s %s ext4 prjquota,nofail 0 2\n' "$UUID" "$POOL_ROOT" \
    > /etc/symphony-pilot/storage.fstab.tmp
install -o root -g root -m 0644 /etc/symphony-pilot/storage.fstab.tmp \
    /etc/fstab.d/symphony-pilot-storage
rm -f /etc/symphony-pilot/storage.fstab.tmp

echo "provisioned fixed Symphony pool; run the trusted Pilot verifier before admission"
