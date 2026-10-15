"""
Root device resolution in rootfs-expand.sh.

The property that matters: the path findmnt reports for / is whatever the
initramfs mounted, and that is not always a device node. A Raspberry Pi boots
with root=/dev/mmcblk0p2 and gives a real node, which is why this worked there
and nowhere else. Every meta-qcom machine boots with root=PARTLABEL=rootfs and
mounts through /dev/disk/by-partlabel/rootfs, which matched none of the
patterns: expansion failed on every boot with "Unsupported device format" and
left the rootfs at its built size (0.42 GB of a 30 GB partition on an Arduino
UNO Q).

The function is extracted from the script and run in /bin/sh, so these tests
exercise the real shell code rather than a reimplementation of it.

Run: pytest tests/test_rootfs_expand.py -v
"""
import os
import re
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "recipes-support", "rootfs-expand", "files", "rootfs-expand.sh")


def shell_func(name):
    """The named shell function, lifted out of the script."""
    src = open(SCRIPT).read()
    m = re.search(r"^%s\(\) \{.*?^\}" % re.escape(name), src, re.S | re.M)
    assert m, "%s not found in rootfs-expand.sh" % name
    return m.group(0)


def resolve(part):
    """Run root_device_and_partition against part; returns (rc, stdout)."""
    script = shell_func("root_device_and_partition") + '\nroot_device_and_partition "$1"\n'
    p = subprocess.run(["/bin/sh", "-c", script, "sh", part],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


class TestRealDeviceNodes:
    """What a Raspberry Pi and friends report: these always worked."""

    @pytest.mark.parametrize("part,dev,num", [
        ("/dev/mmcblk0p2", "/dev/mmcblk0", "2"),
        ("/dev/mmcblk0p38", "/dev/mmcblk0", "38"),
        ("/dev/nvme0n1p3", "/dev/nvme0n1", "3"),
        ("/dev/sda2", "/dev/sda", "2"),
        ("/dev/vdb1", "/dev/vdb", "1"),
    ])
    def test_maps_to_disk_and_partition(self, part, dev, num):
        rc, out = resolve(part)
        assert rc == 0, "refused %s" % part
        assert out == "%s %s" % (dev, num)


class TestPartlabelSymlink:
    """The regression. root=PARTLABEL=rootfs mounts through a symlink."""

    def test_resolves_a_by_partlabel_symlink(self, tmp_path):
        link = tmp_path / "rootfs"
        link.symlink_to("/dev/mmcblk0p38")
        rc, out = resolve(str(link))
        assert rc == 0, "a by-partlabel symlink was refused, which is the bug"
        assert out == "/dev/mmcblk0 38"

    def test_resolves_a_chain_of_symlinks(self, tmp_path):
        first = tmp_path / "by-partlabel-rootfs"
        second = tmp_path / "indirect"
        first.symlink_to("/dev/mmcblk0p38")
        second.symlink_to(first)
        rc, out = resolve(str(second))
        assert rc == 0
        assert out == "/dev/mmcblk0 38"

    def test_resolves_a_by_uuid_style_symlink(self, tmp_path):
        # root=UUID= and root=PARTUUID= land in the same shape.
        link = tmp_path / "51ce1435-ba40-4ace-9886-9bba24fb434f"
        link.symlink_to("/dev/sda3")
        rc, out = resolve(str(link))
        assert rc == 0
        assert out == "/dev/sda 3"


class TestRefusals:
    """Still refuse what cannot be grown, rather than guessing."""

    @pytest.mark.parametrize("part", [
        "/dev/md0",
        "/dev/mapper/root",
        "overlay",
        "/dev/mmcblk0",   # a whole disk, not a partition
        "",
    ])
    def test_refuses(self, part):
        rc, _ = resolve(part)
        assert rc == 1, "accepted %r, which it cannot grow" % part


def needs_expansion(current, device):
    """Run partition_needs_expansion; returns True when it says grow."""
    script = shell_func("partition_needs_expansion") + \
        '\npartition_needs_expansion "$1" "$2"\n'
    p = subprocess.run(["/bin/sh", "-c", script, "sh", str(current), str(device)],
                       capture_output=True, text=True)
    return p.returncode == 0


class TestPartitionExpansionDecision:
    """
    Whether the PARTITION needs growing, which is not the same question as
    whether the FILESYSTEM does.

    The script used to answer "partition is already big" with exit 0 and a
    marker file, so the filesystem resize never ran. Qualcomm's firehose
    patch0.xml grows the last partition to fill the disk at flash time, so on
    those boards the partition arrives full size with the built filesystem
    inside it: a 404 MB filesystem in a 30 GB partition, on every boot, with
    the service reporting success. A Raspberry Pi's partition starts small, so
    it fell through to growpart and the resize happened as a side effect, which
    is why this looked like it worked.
    """

    def test_a_small_partition_should_grow(self):
        assert needs_expansion(400 * 1024**2, 30 * 1024**3) is True

    # The real Arduino UNO Q numbers: a 30.07 GB rootfs partition on a 29.1 GiB
    # eMMC, so 96% of the disk. The partition is done; the filesystem was not.
    def test_the_uno_q_partition_should_not_grow(self):
        assert needs_expansion(58743895 * 512, 31268536320) is False

    def test_exactly_at_the_ten_percent_margin_does_not_grow(self):
        device = 30 * 1024**3
        assert needs_expansion(device * 9 // 10, device) is False

    def test_just_under_the_margin_grows(self):
        device = 30 * 1024**3
        assert needs_expansion(device * 9 // 10 - 1, device) is True

    # No division by zero, and no claiming a growable partition when the disk
    # size could not be read.
    def test_an_unknown_device_size_does_not_grow(self):
        assert needs_expansion(400 * 1024**2, 0) is False

    def test_a_partition_filling_the_disk_does_not_grow(self):
        device = 30 * 1024**3
        assert needs_expansion(device, device) is False


def size_column(df_output):
    """Run df_size_column with df_output on stdin; returns its stdout."""
    script = shell_func("df_size_column") + "\ndf_size_column\n"
    p = subprocess.run(["/bin/sh", "-c", script], input=df_output,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class TestReportedSize:
    """
    Which column of df the completion message reports.

    The script said "New size: $(df -h / | tail -1 | awk '{print $2}')", which
    assumes the device name shares a line with the figures. BusyBox puts a name
    wider than its column on a line of its own, so on an Arduino UNO Q
    (/dev/disk/by-partlabel/rootfs, 29 characters) field 2 of the last line was
    Used, and a resize that had just grown the root filesystem from 373 MB to
    26.6 GB logged "New size: 298.1M". The resize itself was correct; only the
    evidence for it was wrong, which is the harder kind of bug to notice.
    """

    # Verbatim from the board, after the resize succeeded.
    BUSYBOX_WRAPPED = (
        "Filesystem                Size      Used Available Use% Mounted on\n"
        "/dev/disk/by-partlabel/rootfs\n"
        "                         26.6G    298.1M     25.1G   1% /\n"
    )

    # The same board before the resize, to prove the column is not found by
    # looking for something that only a grown filesystem prints.
    BUSYBOX_WRAPPED_SMALL = (
        "Filesystem                Size      Used Available Use% Mounted on\n"
        "/dev/disk/by-partlabel/rootfs\n"
        "                        373.3M    298.1M     46.1M  87% /\n"
    )

    # A short device name, as a Raspberry Pi reports: name and figures on one
    # line. This is the case that always worked and must keep working.
    SINGLE_LINE = (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "/dev/mmcblk0p2   29G  2.1G   26G   8% /\n"
    )

    def test_wrapped_name_reports_size_not_used(self):
        assert size_column(self.BUSYBOX_WRAPPED) == "26.6G"

    def test_wrapped_name_before_the_resize(self):
        assert size_column(self.BUSYBOX_WRAPPED_SMALL) == "373.3M"

    def test_single_line_still_reports_size(self):
        assert size_column(self.SINGLE_LINE) == "29G"

    # GNU coreutils df, which does not wrap, with a name long enough that the
    # old field-2 read would have been Size by luck rather than by rule.
    def test_gnu_df_long_name(self):
        out = (
            "Filesystem                    Size  Used Avail Use% Mounted on\n"
            "/dev/disk/by-partlabel/rootfs   27G  299M   25G   2% /\n"
        )
        assert size_column(out) == "27G"
