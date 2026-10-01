"""Publish one GPIO panel and start its VM when the panel is present."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from workers.gpio.pins import (
    DEFAULT_GPIO_PINS,
    DEFAULT_GPIO_VERSION,
    HEADER_BCM_PINS,
    UART_RESERVED_BCM_PINS,
    build_gpio_map,
    normalize_pins,
    pins_from_fields,
)

from .db import HubRepository
from .discovery import discover_gpio_resources
from .vm_config import normalize_runtime_config


def _header_pins(document: dict) -> set[int]:
    pins: set[int] = set()
    for field in document.get("fields") or []:
        if isinstance(field, dict) and field.get("source") == "pins" and "bcm_pin" in field:
            pins.add(int(field["bcm_pin"]))
    return pins


def _ensure_default_panel(repo: HubRepository) -> str:
    """Publish a UART-safe default map. Saved maps are immutable, so a panel
    that still lists BCM 14–17 cannot be edited in place."""
    existing = repo.map_by_version(DEFAULT_GPIO_VERSION, "gpio") or {}
    if existing and not (_header_pins(existing) & UART_RESERVED_BCM_PINS):
        return DEFAULT_GPIO_VERSION
    if not existing:
        repo.save_map(build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION))
    return DEFAULT_GPIO_VERSION


def _without_uart_pins(document: dict) -> list[dict]:
    return [
        {
            "bcm_pin": pin.bcm_pin,
            "name": pin.name,
            "trigger_level": pin.trigger_level,
            "hold_sec": pin.hold_sec,
            "pull": pin.pull,
            "invert": pin.invert,
        }
        for pin in pins_from_fields(document.get("fields") or [])
    ]


def _safe_map_version(repo: HubRepository, document: dict, current_version: str) -> str:
    default = _ensure_default_panel(repo)
    raw_pins = _header_pins(document)
    if not raw_pins:
        return default
    if not raw_pins & UART_RESERVED_BCM_PINS:
        if current_version in {"gpio-default-v1", "gpio-panel-v1", "gpio-panel-v2"} and set(HEADER_BCM_PINS).issubset(raw_pins):
            return default
        return current_version
    safe_pins = _without_uart_pins(document)
    stock = raw_pins == set(range(2, 28)) or current_version in {
        "gpio-default-v1",
        "gpio-panel-v1",
        "gpio-panel-v2",
        DEFAULT_GPIO_VERSION,
    }
    if not safe_pins or stock:
        return default
    digest = hashlib.sha256(
        json.dumps(safe_pins, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    version = f"gpio-uart-safe-{digest}"
    if repo.map_by_version(version, "gpio") is None:
        repo.save_map(build_gpio_map(safe_pins, version))
    return version


def ensure_gpio_vm(repo: HubRepository, *, worker_image: str) -> list[str]:
    """Seed or repair the GPIO panel. Returns VM ids whose map changed."""
    panel = discover_gpio_resources()
    if not panel:
        return []
    path = str(panel[0].get("path") or "").strip()
    if not path:
        return []
    default = _ensure_default_panel(repo)
    existing = [vm for vm in repo.list_vms() if str(vm.get("protocol") or "") == "gpio"]
    if not existing:
        config = normalize_runtime_config(
            {"reader": {"poll_interval_sec": 0.05, "gpio_chip": path, "enabled": True}},
            protocol="gpio",
        )
        repo.create_vm(
            {
                "name": "GPIO Raspberry",
                "description": "GPIO панель: BCM 2–27, кроме UART 14–17",
                "protocol": "gpio",
                "preset_id": "gpio-raspberry",
                "map_version": default,
                "read_resources": [],
                "storage_resource_id": "storage:data",
                "config": config,
                "worker_image": worker_image,
                "desired_state": "running",
            }
        )
        return []
    reloaded: list[str] = []
    for vm in existing:
        document = repo.map_by_version(str(vm.get("map_version") or ""), "gpio") or {}
        reader = ((vm.get("config") or {}).get("reader") or {}) if isinstance(vm.get("config"), dict) else {}
        updates: dict = {}
        current_version = str(vm.get("map_version") or "")
        safe_version = _safe_map_version(repo, document, current_version)
        if safe_version != current_version:
            updates["map_version"] = safe_version
        if vm.get("read_resources") or str(reader.get("gpio_chip") or "") != path:
            updates["read_resources"] = []
            updates["config"] = normalize_runtime_config(
                {"reader": {**reader, "gpio_chip": path, "enabled": reader.get("enabled", True)}},
                protocol="gpio",
            )
        if updates:
            repo.update_vm(str(vm["id"]), updates)
            if "map_version" in updates:
                reloaded.append(str(vm["id"]))
    return reloaded


def publish_gpio_pins(repo: HubRepository, pins: list[dict], *, current_version: str) -> str:
    normalized = normalize_pins(pins)
    current = repo.map_by_version(current_version, "gpio") or {}
    current_pins = [
        {
            "bcm_pin": pin.bcm_pin,
            "name": pin.name,
            "trigger_level": pin.trigger_level,
            "hold_sec": pin.hold_sec,
            "pull": pin.pull,
            "invert": pin.invert,
        }
        for pin in pins_from_fields(current.get("fields") or [])
    ]
    if current_pins == normalized:
        return current_version
    version = "gpio-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    repo.save_map(build_gpio_map(normalized, version))
    return version
