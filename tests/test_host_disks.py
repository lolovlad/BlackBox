from __future__ import annotations

from pathlib import Path

from services.hub.discovery import discover_storage_resources
from services.hub.disks import dashboard_disks


def _sectors(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(size_bytes // 512), encoding="utf-8")


def _pi_layout(root: Path) -> tuple[Path, Path]:
    block = root / "sys" / "block"
    gib = 1024**3
    _sectors(block / "mmcblk0" / "size", int(59.5 * gib))
    _sectors(block / "mmcblk0" / "mmcblk0p1" / "size", 512 * 1024**2)
    _sectors(block / "mmcblk0" / "mmcblk0p2" / "size", 59 * gib)
    _sectors(block / "nvme0n1" / "size", int(465.8 * gib))
    _sectors(block / "nvme0n1" / "nvme0n1p1" / "size", int(465.8 * gib))
    _sectors(block / "zram0" / "size", 2 * gib)
    _sectors(block / "loop0" / "size", 2 * gib)
    mountinfo = root / "mountinfo"
    mountinfo.write_text(
        "\n".join(
            [
                "22 1 179:1 / /boot/firmware rw - vfat /dev/mmcblk0p1 rw",
                "23 1 179:2 / / rw - ext4 /dev/mmcblk0p2 rw",
                "24 1 179:2 / /etc/hosts rw - ext4 /dev/mmcblk0p2 rw",
                "25 1 179:2 / /data rw - ext4 /dev/mmcblk0p2 rw",
                "26 1 179:2 / /etc/resolv.conf rw - ext4 /dev/mmcblk0p2 rw",
                "30 1 259:1 / /mnt/nvme rw - ext4 /dev/nvme0n1p1 rw",
                "31 1 254:0 / /swap rw - swap /dev/zram0 rw",
                "32 1 7:0 / /snap/core rw - squashfs /dev/loop0 rw",
                "33 1 0:45 / / rw - overlay overlay rw",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return block, mountinfo


def test_dashboard_shows_each_physical_disk_once(tmp_path: Path) -> None:
    block, mountinfo = _pi_layout(tmp_path)
    cards = dashboard_disks(sys_block=block, mountinfo=mountinfo, visible=lambda _host: None)
    assert cards is not None
    devices = [card["device"] for card in cards]
    assert devices == ["SD-карта · mmcblk0", "NVMe SSD · nvme0n1"]
    nvme = cards[1]
    assert nvme["mount"] == "/mnt/nvme"
    assert nvme["percent"] is None
    sd = cards[0]
    assert sd["mount"] == "/"


def test_storage_scan_finds_nvme_once_and_skips_virtual(tmp_path: Path) -> None:
    block, mountinfo = _pi_layout(tmp_path)
    visible_root = tmp_path / "nvme"
    visible_root.mkdir()

    hidden = discover_storage_resources(
        tmp_path / "data",
        sys_block=block,
        mountinfo=mountinfo,
        visible=lambda host: visible_root if host == "/mnt/nvme" else None,
    )
    by_id = {item["resource_id"]: item for item in hidden}
    assert "storage:data" in by_id
    assert "storage:nvme0n1p1" in by_id
    assert by_id["storage:nvme0n1p1"]["name"] == "NVMe SSD"
    assert by_id["storage:nvme0n1p1"]["available"] is True
    assert by_id["storage:nvme0n1p1"]["path"] == str(visible_root)
    assert "storage:zram0" not in by_id
    assert "storage:loop0" not in by_id
    assert "storage:mmcblk0p2" not in by_id
    assert sum(1 for item in hidden if item["resource_id"].startswith("storage:nvme")) == 1

    missing = discover_storage_resources(
        tmp_path / "data-2",
        sys_block=block,
        mountinfo=mountinfo,
        visible=lambda _host: None,
    )
    nvme = next(item for item in missing if item["resource_id"] == "storage:nvme0n1p1")
    assert nvme["available"] is False
    assert nvme["path"] == "/mnt/nvme"
    assert "не проброшен" in nvme["metadata"]["detail"]


def test_unmounted_nvme_is_listed_unavailable(tmp_path: Path) -> None:
    block = tmp_path / "sys" / "block"
    _sectors(block / "nvme0n1" / "size", 100 * 1024**3)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    found = discover_storage_resources(tmp_path / "data", sys_block=block, mountinfo=mountinfo, visible=lambda _host: None)
    nvme = next(item for item in found if item["resource_id"] == "storage:nvme0n1")
    assert nvme["available"] is False
    assert "не смонтирован" in nvme["metadata"]["detail"]
