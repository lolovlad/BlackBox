"""Host panel for the machine running the Hub (the legacy Raspberry view)."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def collect_system_monitor(data_root: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "disk": {"used_gb": None, "total_gb": None, "free_gb": None, "percent": None},
        "cpu": {"percent": None, "cores_logical": None, "cores_physical": None, "fan_rpm": None},
        "memory": {"used_gb": None, "total_gb": None, "percent": None},
        "process": {"pid": os.getpid(), "uptime_sec": None},
        "disks": [],
    }
    try:
        target = data_root if data_root.exists() else data_root.parent
        usage = shutil.disk_usage(target)
        used = usage.total - usage.free
        stats["disk"] = {
            "used_gb": round(used / (1024**3), 2),
            "total_gb": round(usage.total / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent": round((used / usage.total) * 100.0, 1) if usage.total else 0.0,
        }
    except OSError:
        pass

    try:
        import psutil

        memory = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        stats["cpu"] = {
            "percent": round(psutil.cpu_percent(interval=None), 1),
            "cores_logical": int(psutil.cpu_count(logical=True) or 0),
            "cores_physical": int(psutil.cpu_count(logical=False) or 0),
            "fan_rpm": _cpu_fan_rpm(psutil),
        }
        stats["memory"] = {
            "used_gb": round((memory.total - memory.available) / (1024**3), 2),
            "total_gb": round(memory.total / (1024**3), 2),
            "percent": round(float(memory.percent), 1),
        }
        stats["process"]["uptime_sec"] = int(max(0.0, time.time() - process.create_time()))
        stats["disks"] = _dashboard_disks(psutil)
    except Exception:
        pass
    return stats


_PSEUDO_FS = frozenset(
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
_SKIP_MOUNT = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})


def _dashboard_disks(psutil: Any) -> list[dict[str, Any]]:
    from .discovery import _path_from_env
    from .disks import dashboard_disks, parent_disk_name

    sys_block = _path_from_env("BB_DISCOVERY_SYS_BLOCK", "/host/sys/block", "/sys/block")
    mountinfo = _path_from_env("BB_DISCOVERY_MOUNTINFO", "/host/proc/1/mountinfo", "/proc/1/mountinfo", "/proc/self/mountinfo")
    host = dashboard_disks(sys_block=sys_block, mountinfo=mountinfo)
    if host is not None:
        return host
    grouped: dict[str, dict[str, Any]] = {}
    for part in psutil.disk_partitions(all=False):
        mount = str(getattr(part, "mountpoint", "") or "")
        device = str(getattr(part, "device", "") or "")
        fstype = str(getattr(part, "fstype", "") or "")
        if not mount or fstype in _PSEUDO_FS or mount in _SKIP_MOUNT:
            continue
        if mount.startswith("/etc/") or mount.startswith("/proc") or mount.startswith("/sys") or mount.startswith("/dev"):
            continue
        node = device.removeprefix("/dev/")
        if node.startswith(("loop", "zram", "ram")):
            continue
        try:
            mount_usage = psutil.disk_usage(mount)
        except (OSError, PermissionError):
            continue
        parent = parent_disk_name(node) if device.startswith("/dev/") else device or mount
        card = {
            "mount": mount,
            "device": device or mount,
            "fstype": fstype,
            "used_gb": round((mount_usage.total - mount_usage.free) / (1024**3), 2),
            "total_gb": round(mount_usage.total / (1024**3), 2),
            "free_gb": round(mount_usage.free / (1024**3), 2),
            "percent": round(float(mount_usage.percent), 1),
        }
        current = grouped.get(parent)
        if current is None or card["total_gb"] > current["total_gb"]:
            grouped[parent] = card
    return list(grouped.values())


def _cpu_fan_rpm(psutil: Any) -> int | None:
    try:
        fans = psutil.sensors_fans()
    except Exception:
        return None
    selected: int | None = None
    for source_name, entries in fans.items():
        for entry in entries:
            label = (getattr(entry, "label", "") or "").lower()
            current = getattr(entry, "current", None)
            if current is None:
                continue
            if "cpu" in label or "proc" in label or "cpu" in str(source_name).lower():
                return int(current)
            if selected is None:
                selected = int(current)
    return selected


def gpio_panel(vms: list[dict[str, Any]], latest_by_vm: dict[str, Any]) -> dict[str, Any]:
    """Active GPIO pins from every GPIO source, matching the legacy alert list."""
    items: list[dict[str, Any]] = []
    updated_at: datetime | None = None
    for vm in vms:
        if str(vm.get("protocol") or "") != "gpio":
            continue
        sample = latest_by_vm.get(str(vm.get("id")))
        if sample is None:
            continue
        captured = getattr(sample, "captured_at", None)
        if isinstance(captured, datetime) and (updated_at is None or captured > updated_at):
            updated_at = captured
        discrete = getattr(sample, "discrete", None) or {}
        if not isinstance(discrete, dict):
            continue
        for name, value in discrete.items():
            if not value:
                continue
            items.append(
                {
                    "vm_id": str(vm.get("id")),
                    "vm_name": str(vm.get("name") or vm.get("id")),
                    "name": str(name),
                    "is_on": True,
                }
            )
    return {
        "items": items,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }
