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
