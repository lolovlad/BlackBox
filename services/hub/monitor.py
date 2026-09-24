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
        disks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for part in psutil.disk_partitions(all=False):
            mount = str(getattr(part, "mountpoint", "") or "")
            if not mount or mount in seen:
                continue
            seen.add(mount)
            try:
                mount_usage = psutil.disk_usage(mount)
            except (OSError, PermissionError):
                continue
            disks.append(
                {
                    "mount": mount,
                    "device": str(getattr(part, "device", "") or ""),
                    "fstype": str(getattr(part, "fstype", "") or ""),
                    "used_gb": round((mount_usage.total - mount_usage.free) / (1024**3), 2),
                    "total_gb": round(mount_usage.total / (1024**3), 2),
                    "free_gb": round(mount_usage.free / (1024**3), 2),
                    "percent": round(float(mount_usage.percent), 1),
                }
            )
        stats["disks"] = disks
    except Exception:
        pass
    return stats


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
