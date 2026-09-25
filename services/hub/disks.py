"""Host block devices for the storage scanner and the dashboard.

The Hub often runs in a container, so ``psutil`` only sees container mounts.
Physical disks are read from the host sysfs and the host mount table, which
Compose bind-mounts at ``/host/sys/block`` and ``/host/proc/1/mountinfo``.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

_SKIP_DISK = re.compile(r"^(loop|zram|ram|dm-|fd|sr|nbd|zd)", re.IGNORECASE)
_PART_PARENT = (
    re.compile(r"^(nvme\d+n\d+)p\d+$"),
    re.compile(r"^(mmcblk\d+)p\d+$"),
    re.compile(r"^((?:sd|hd|vd|xvd)[a-z]+)\d+$"),
)
_SKIP_FS = frozenset(
    {
        "autofs",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "fusectl",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)
_SKIP_MOUNT_EXACT = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})
_SYSTEM_MOUNTS = frozenset({"/", "/boot", "/boot/firmware"})
_OCTAL = re.compile(r"\\([0-7]{3})")


def unescape_mount(value: str) -> str:
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), value)


def parent_disk_name(name: str) -> str:
    text = str(name or "").strip()
    if text.startswith("/dev/"):
        text = text[5:]
    for pattern in _PART_PARENT:
        match = pattern.match(text)
        if match:
            return match.group(1)
    return text


def disk_title(name: str) -> str:
    if name.startswith("nvme"):
        return "NVMe SSD"
    if name.startswith("mmcblk"):
        return "SD-карта"
    if re.match(r"^(?:sd|hd|vd|xvd)", name):
        return "Диск"
    return name


def parse_mountinfo(text: str) -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or " - " not in line:
            continue
        left, _, right = line.partition(" - ")
        left_parts = left.split()
        right_parts = right.split()
        if len(left_parts) < 5 or len(right_parts) < 2:
            continue
        mounts.append(
            {
                "major_minor": left_parts[2],
                "mountpoint": unescape_mount(left_parts[4]),
                "fstype": right_parts[0],
                "source": unescape_mount(right_parts[1]),
            }
        )
    return mounts


def _sector_bytes(path: Path) -> int | None:
    try:
        sectors = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None
    if sectors < 0:
        return None
    return sectors * 512


def read_sys_block(root: Path) -> dict[str, dict[str, Any]]:
    disks: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return disks
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return disks
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir or _SKIP_DISK.match(entry.name):
            continue
        size = _sector_bytes(entry / "size")
        if size is None:
            continue
        partitions: list[dict[str, Any]] = []
        try:
            children = sorted(entry.iterdir())
        except OSError:
            children = []
        for child in children:
            try:
                if not child.is_dir() or not (child / "size").is_file():
                    continue
            except OSError:
                continue
            if not child.name.startswith(entry.name):
                continue
            part_size = _sector_bytes(child / "size")
            if part_size is None:
                continue
            partitions.append({"name": child.name, "size_bytes": part_size})
        disks[entry.name] = {"name": entry.name, "size_bytes": size, "partitions": partitions}
    return disks


def skip_mount(mountpoint: str, fstype: str, source: str) -> bool:
    point = str(mountpoint or "")
    kind = str(fstype or "")
    device = str(source or "")
    if not point or kind in _SKIP_FS:
        return True
    if point in _SKIP_MOUNT_EXACT or point.startswith("/etc/") or point.startswith("/proc") or point.startswith("/sys") or point.startswith("/dev"):
        return True
    if point == "/run" or (point.startswith("/run/") and not point.startswith("/run/media")):
        return True
    if not device.startswith("/dev/"):
        return True
    node = device.removeprefix("/dev/")
    if _SKIP_DISK.match(node):
        return True
    return False


def _gb(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**3), 2)


def _mounted_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.path.ismount(path)
    except OSError:
        return False


def default_visible_path(host_mount: str) -> Path | None:
    """Map a host mountpoint to a directory this process can stat."""
    direct = Path(host_mount)
    if _mounted_dir(direct):
        return direct
    from .discovery import _path_from_env

    mappings = (
        ("/mnt", "BB_DISCOVERY_HOST_MNT", ("/host/mnt", "/mnt")),
        ("/media", "BB_DISCOVERY_HOST_MEDIA", ("/host/media", "/media")),
        ("/run/media", "BB_DISCOVERY_HOST_RUN_MEDIA", ("/host/run/media", "/run/media")),
    )
    for prefix, env_name, defaults in mappings:
        if host_mount != prefix and not host_mount.startswith(prefix + "/"):
            continue
        root = _path_from_env(env_name, *defaults)
        rest = host_mount[len(prefix) :].lstrip("/")
        candidate = root / rest if rest else root
        if _mounted_dir(candidate):
            return candidate
    return None


def _usage(path: Path) -> tuple[int, int, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return int(usage.total), int(usage.used), int(usage.free)


def _dev_id(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _prefer_mount(items: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(item: dict[str, Any]) -> tuple[int, int]:
        point = str(item.get("mountpoint") or "")
        if point == "/":
            return (0, len(point))
        if point.startswith("/mnt") or point.startswith("/media") or point.startswith("/run/media"):
            return (1, len(point))
        return (2, len(point))

    return sorted(items, key=rank)[0]


def _partition_size(disks: dict[str, dict[str, Any]], node: str) -> int:
    parent = parent_disk_name(node)
    disk = disks.get(parent) or {}
    for part in disk.get("partitions") or []:
        if part["name"] == node:
            return int(part["size_bytes"])
    if parent == node:
        return int(disk.get("size_bytes") or 0)
    return 0


def load_host_view(
    *,
    sys_block: Path | None,
    mountinfo: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]] | None:
    if sys_block is None or not sys_block.is_dir():
        return None
    text = ""
    if mountinfo is not None and mountinfo.is_file():
        try:
            text = mountinfo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    disks = read_sys_block(sys_block)
    mounts = [item for item in parse_mountinfo(text) if not skip_mount(item["mountpoint"], item["fstype"], item["source"])]
    for item in mounts:
        node = item["source"].removeprefix("/dev/")
        item["node"] = node
        item["parent"] = parent_disk_name(node)
        item["size_bytes"] = _partition_size(disks, node)
    return disks, mounts


def dashboard_disks(
    *,
    sys_block: Path | None = None,
    mountinfo: Path | None = None,
    visible: Callable[[str], Path | None] | None = None,
) -> list[dict[str, Any]] | None:
    """One card per physical disk. ``None`` when the host sysfs is not mounted."""
    loaded = load_host_view(sys_block=sys_block, mountinfo=mountinfo)
    if loaded is None:
        return None
    disks, mounts = loaded
    lookup = visible or default_visible_path
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in mounts:
        grouped.setdefault(str(item["parent"]), []).append(item)
    names = list(disks)
    for parent in grouped:
        if parent not in names:
            names.append(parent)
    cards: list[dict[str, Any]] = []
    for name in names:
        if _SKIP_DISK.match(name):
            continue
        disk = disks.get(name) or {"name": name, "size_bytes": 0, "partitions": []}
        owned = grouped.get(name) or []
        if not owned and not disk.get("size_bytes"):
            continue
        chosen: dict[str, Any] | None = None
        if owned:
            largest = max(int(item.get("size_bytes") or 0) for item in owned)
            pool = [item for item in owned if int(item.get("size_bytes") or 0) == largest] or owned
            chosen = _prefer_mount(pool)
        mountpoint = str(chosen["mountpoint"]) if chosen else "не смонтирован"
        fstype = str(chosen["fstype"]) if chosen else ""
        visible_path = lookup(mountpoint) if chosen else None
        usage = _usage(visible_path) if visible_path is not None else None
        if usage is not None:
            total, used, free = usage
            percent = round((used / total) * 100.0, 1) if total else 0.0
        else:
            total = int(chosen["size_bytes"]) if chosen and chosen.get("size_bytes") else int(disk.get("size_bytes") or 0)
            used = None
            free = None
            percent = None
        cards.append(
            {
                "mount": mountpoint,
                "device": f"{disk_title(name)} · {name}",
                "fstype": fstype or "—",
                "used_gb": _gb(used),
                "total_gb": _gb(total),
                "free_gb": _gb(free),
                "percent": percent,
            }
        )
    return cards


def _external_mount(item: dict[str, Any]) -> bool:
    point = str(item.get("mountpoint") or "")
    parent = str(item.get("parent") or "")
    if point in _SYSTEM_MOUNTS or point.startswith("/boot/"):
        return False
    if point.startswith("/mnt") or point.startswith("/media") or point.startswith("/run/media"):
        return True
    return parent.startswith("nvme")


def storage_filesystems(
    *,
    sys_block: Path | None,
    mountinfo: Path | None,
    visible: Callable[[str], Path | None] | None = None,
) -> list[dict[str, Any]]:
    """One storage target per host filesystem, plus unmounted NVMe disks."""
    loaded = load_host_view(sys_block=sys_block, mountinfo=mountinfo)
    if loaded is None:
        return []
    disks, mounts = loaded
    lookup = visible or default_visible_path
    by_device: dict[str, list[dict[str, Any]]] = {}
    for item in mounts:
        if not _external_mount(item):
            continue
        by_device.setdefault(str(item["major_minor"]), []).append(item)
    found: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for group in by_device.values():
        chosen = _prefer_mount(group)
        node = str(chosen["node"])
        seen_nodes.add(node)
        parent = str(chosen["parent"])
        seen_nodes.add(parent)
        host_mount = str(chosen["mountpoint"])
        visible_path = lookup(host_mount)
        title = disk_title(parent)
        siblings = [item for item in by_device.values() if item and item[0].get("parent") == parent]
        name = title if len(siblings) <= 1 else f"{title} · {node}"
        usage = _usage(visible_path) if visible_path is not None else None
        available = usage is not None
        path = str(visible_path) if visible_path is not None else host_mount
        if available and usage is not None:
            total, _used, free = usage
            detail = None
        else:
            total = int(chosen.get("size_bytes") or 0) or None
            free = None
            detail = f"виден на хосте в {host_mount}, каталог не проброшен в Hub"
        found.append(
            {
                "resource_id": f"storage:{node}",
                "name": name,
                "path": path,
                "available": available,
                "host_mount": host_mount,
                "device": node,
                "parent": parent,
                "fstype": chosen.get("fstype") or "",
                "total_bytes": total,
                "free_bytes": free,
                "detail": detail,
            }
        )
    for name, disk in disks.items():
        if not name.startswith("nvme") or name in seen_nodes:
            continue
        if any(part["name"] in seen_nodes for part in disk.get("partitions") or []):
            continue
        found.append(
            {
                "resource_id": f"storage:{name}",
                "name": disk_title(name),
                "path": "",
                "available": False,
                "host_mount": "",
                "device": name,
                "parent": name,
                "fstype": "",
                "total_bytes": int(disk.get("size_bytes") or 0) or None,
                "free_bytes": None,
                "detail": "NVMe виден в системе, но не смонтирован",
            }
        )
    return found


def dedupe_key(path: str) -> str:
    resolved = str(Path(path).resolve()) if path else ""
    ident = _dev_id(Path(path)) if path else None
    return f"{ident}:{resolved}"
