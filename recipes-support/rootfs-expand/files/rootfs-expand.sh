#!/bin/sh
# SPDX-License-Identifier: MIT
#
# rootfs-expand.sh - Expand root filesystem to use all available space
#
# This script automatically expands the root partition and filesystem
# on first boot to utilize the full SD card/storage capacity.
#
# Copyright (c) 2026 Marco Pennelli <marco.pennelli@technosec.net>

set -e

MARKER_FILE="/var/lib/rootfs-expand/.expanded"
LOG_TAG="rootfs-expand"

log_info() {
    logger -t "$LOG_TAG" -p info "$1"
    echo "[INFO] $1"
}

log_error() {
    logger -t "$LOG_TAG" -p err "$1"
    echo "[ERROR] $1" >&2
}

# Check if already expanded
if [ -f "$MARKER_FILE" ]; then
    log_info "Root filesystem already expanded, skipping"
    exit 0
fi

# Find root device and partition
ROOT_PART=$(findmnt -n -o SOURCE /)
if [ -z "$ROOT_PART" ]; then
    log_error "Could not determine root partition"
    exit 1
fi

log_info "Root partition: $ROOT_PART"

# Map the path root was mounted from to its disk and partition number.
#
# findmnt reports whatever the initramfs mounted root from, which is not always
# a device node. A board whose kernel command line says root=/dev/mmcblk0p2, as
# a Raspberry Pi does, gives a real node and matches the patterns below
# directly. A board whose command line says root=PARTLABEL=rootfs, as every
# meta-qcom machine does, mounts root through /dev/disk/by-partlabel/rootfs
# instead: the patterns then matched nothing, expansion failed on every boot
# with "Unsupported device format", and the rootfs stayed at its built size on
# a disk many times larger. On an Arduino UNO Q that was 0.42 GB of a 30 GB
# partition. So resolve any symlink before matching.
#
# Echoes "<disk> <partition-number>", and returns 1 for a path this script does
# not know how to grow. No logging in here: it is called from the test suite.
root_device_and_partition() {
    _part=$1

    _canon=$(readlink -f "$_part" 2>/dev/null) || _canon=""
    if [ -n "$_canon" ]; then
        _part=$_canon
    fi

    case "$_part" in
        /dev/mmcblk*p* | /dev/nvme*n*p*)
            echo "$(echo "$_part" | sed 's/p[0-9]*$//') $(echo "$_part" | sed 's/.*p//')"
            ;;
        /dev/sd* | /dev/vd*)
            echo "$(echo "$_part" | sed 's/[0-9]*$//') $(echo "$_part" | sed 's/.*[a-z]//')"
            ;;
        *)
            return 1
            ;;
    esac
}

RESOLVED=$(root_device_and_partition "$ROOT_PART") || {
    log_error "Unsupported device format: $ROOT_PART"
    exit 1
}
ROOT_DEV=${RESOLVED% *}
PART_NUM=${RESOLVED#* }

log_info "Root device: $ROOT_DEV, partition number: $PART_NUM"

# Get current and max partition sizes
# Use head -1 to get only the device/partition size (not children)
# Use tr to remove any trailing whitespace
CURRENT_SIZE=$(lsblk -b -n -d -o SIZE "$ROOT_PART" 2>/dev/null | tr -d '[:space:]')
DEVICE_SIZE=$(lsblk -b -n -d -o SIZE "$ROOT_DEV" 2>/dev/null | tr -d '[:space:]')

# Ensure we have valid numbers, default to 0 if empty
CURRENT_SIZE=${CURRENT_SIZE:-0}
DEVICE_SIZE=${DEVICE_SIZE:-0}

log_info "Current partition size: $CURRENT_SIZE bytes"
log_info "Device size: $DEVICE_SIZE bytes"

# Check if expansion is needed (leave 10% margin)
# Use shell arithmetic instead of bc for portability
# Ensure DEVICE_SIZE is non-zero to avoid division issues
if [ "$DEVICE_SIZE" -eq 0 ] 2>/dev/null; then
    log_error "Could not determine device size"
    exit 1
fi

# The size column of a df report arriving on stdin.
#
# BusyBox df prints a device name wider than its column on a line of its own and
# the figures on the next, so the last line of "df -h /" can read
#
#     26.6G    298.1M     25.1G   1% /
#
# with no device name in it at all. Field 2 is then Used, not Size, and this
# reported a freshly grown 26.6 GB root filesystem as "New size: 298.1M" on an
# Arduino UNO Q. Counting back from the end of the line picks the same column
# whether the name wrapped or not, because the five figures after it always
# follow.
#
# Echoes the size column. No logging in here: it is called from the test suite.
df_size_column() {
    tail -1 | awk '{ print $(NF - 4) }'
}

# Whether the partition itself still has room to grow, with a 10% margin.
#
# A false answer must NOT end the run, and treating it as "nothing to do" was a
# bug. The filesystem inside a partition can be far smaller than the partition,
# which is the normal state after flashing an image whose partition was grown by
# the flashing tool rather than by this script: Qualcomm's firehose patch0.xml
# grows the last partition to fill the disk at flash time, so the partition
# arrives at full size with the built filesystem inside it. This used to log
# "Partition already at maximum size, no expansion needed" and exit 0, leaving a
# 404 MB filesystem inside a 30 GB partition on every boot. Only a board whose
# partition starts small, such as a Raspberry Pi, ever reached the resize below.
#
# Echoes nothing; returns 0 when the partition should be grown.
partition_needs_expansion() {
    _cur=$1
    _dev=$2
    [ "$_dev" -gt 0 ] 2>/dev/null || return 1
    [ "$_cur" -lt "$((_dev * 9 / 10))" ] 2>/dev/null
}

# Expand the partition only if it has room. Either way the filesystem resize
# below still runs, because that is the part that was never happening.
PARTITION_CHANGED=0
if ! partition_needs_expansion "$CURRENT_SIZE" "$DEVICE_SIZE"; then
    log_info "Partition already uses the disk, skipping partition expansion"
elif command -v growpart >/dev/null 2>&1; then
    log_info "Expanding partition using growpart..."
    if growpart "$ROOT_DEV" "$PART_NUM"; then
        log_info "Partition expanded successfully"
        PARTITION_CHANGED=1
    else
        # growpart returns 1 if partition is already at max size
        GROW_RC=$?
        if [ "$GROW_RC" -eq 1 ]; then
            log_info "Partition already at maximum size"
        else
            log_error "growpart failed with exit code $GROW_RC"
            exit 1
        fi
    fi
elif command -v sfdisk >/dev/null 2>&1; then
    # Fallback to sfdisk
    log_info "Expanding partition using sfdisk..."
    echo ",+" | sfdisk -N "$PART_NUM" "$ROOT_DEV" --no-reread 2>/dev/null || true
    PARTITION_CHANGED=1
else
    log_error "Neither growpart nor sfdisk available for partition expansion"
    exit 1
fi

# Inform kernel of partition table changes
# This is critical for resize2fs to see the new partition size
if [ "$PARTITION_CHANGED" -eq 1 ]; then
    log_info "Updating kernel partition table..."

    # Try partprobe first (from parted)
    if command -v partprobe >/dev/null 2>&1; then
        partprobe "$ROOT_DEV" 2>/dev/null || true
    fi

    # Also try partx to update specific partition (more reliable for mounted partitions)
    if command -v partx >/dev/null 2>&1; then
        partx -u "$ROOT_PART" 2>/dev/null || true
    fi

    # Small delay to let kernel process the changes
    sleep 1

    # Verify the kernel sees the new size
    NEW_PART_SIZE=$(lsblk -b -n -d -o SIZE "$ROOT_PART" 2>/dev/null | tr -d '[:space:]')
    log_info "Kernel now sees partition size: $NEW_PART_SIZE bytes"
fi

# Resize the filesystem
log_info "Resizing filesystem..."

# Detect filesystem type
FS_TYPE=$(lsblk -n -o FSTYPE "$ROOT_PART" 2>/dev/null || blkid -s TYPE -o value "$ROOT_PART" 2>/dev/null)

case "$FS_TYPE" in
    ext2|ext3|ext4)
        log_info "Detected ext filesystem, using resize2fs"
        if resize2fs "$ROOT_PART"; then
            log_info "Filesystem resized successfully"
        else
            log_error "resize2fs failed"
            exit 1
        fi
        ;;
    btrfs)
        log_info "Detected btrfs filesystem"
        if btrfs filesystem resize max /; then
            log_info "Filesystem resized successfully"
        else
            log_error "btrfs resize failed"
            exit 1
        fi
        ;;
    xfs)
        log_info "Detected XFS filesystem"
        if xfs_growfs /; then
            log_info "Filesystem resized successfully"
        else
            log_error "xfs_growfs failed"
            exit 1
        fi
        ;;
    f2fs)
        log_info "Detected F2FS filesystem"
        if resize.f2fs "$ROOT_PART"; then
            log_info "Filesystem resized successfully"
        else
            log_error "resize.f2fs failed"
            exit 1
        fi
        ;;
    *)
        log_error "Unsupported filesystem type: $FS_TYPE"
        exit 1
        ;;
esac

# Create marker file to prevent re-running
mkdir -p "$(dirname "$MARKER_FILE")"
touch "$MARKER_FILE"

# Log final size
NEW_SIZE=$(df -h / | df_size_column)
log_info "Root filesystem expansion complete. New size: $NEW_SIZE"

exit 0
