"""Publish one GPIO panel and start its VM when the panel is present."""

from __future__ import annotations

from datetime import datetime, timezone

from workers.gpio.pins import DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION, HEADER_BCM_PINS, build_gpio_map, normalize_pins, pins_from_fields

from .db import HubRepository
from .discovery import discover_gpio_resources
from .vm_config import normalize_runtime_config


def _header_pins(document: dict) -> set[int]:
    pins: set[int] = set()
    for field in document.get("fields") or []:
        if isinstance(field, dict) and field.get("source") == "pins" and "bcm_pin" in field:
            pins.add(int(field["bcm_pin"]))
    return pins


def _needs_panel_map(document: dict, version: str) -> bool:
    pins = _header_pins(document)
    if set(HEADER_BCM_PINS).issubset(pins):
        return False
    if version == "gpio-default-v1" or len(pins) <= 1:
        return True
    return False


def ensure_gpio_vm(repo: HubRepository, *, worker_image: str) -> None:
    panel = discover_gpio_resources()
    if not panel:
        return
    path = str(panel[0].get("path") or "").strip()
    if not path:
        return
    if repo.map_by_version(DEFAULT_GPIO_VERSION, "gpio") is None:
        repo.save_map(build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION))
    existing = [vm for vm in repo.list_vms() if str(vm.get("protocol") or "") == "gpio"]
    if not existing:
        config = normalize_runtime_config(
            {"reader": {"poll_interval_sec": 0.05, "gpio_chip": path, "enabled": True}},
            protocol="gpio",
        )
        repo.create_vm(
            {
                "name": "GPIO Raspberry",
                "description": "GPIO панель: все пины BCM 2–27",
                "protocol": "gpio",
                "preset_id": "gpio-raspberry",
                "map_version": DEFAULT_GPIO_VERSION,
                "read_resources": [],
                "storage_resource_id": "storage:data",
                "config": config,
                "worker_image": worker_image,
                "desired_state": "running",
            }
        )
        return
    vm = existing[0]
    document = repo.map_by_version(str(vm.get("map_version") or ""), "gpio") or {}
    reader = ((vm.get("config") or {}).get("reader") or {}) if isinstance(vm.get("config"), dict) else {}
    updates: dict = {}
    if _needs_panel_map(document, str(vm.get("map_version") or "")):
        updates["map_version"] = DEFAULT_GPIO_VERSION
    if vm.get("read_resources") or str(reader.get("gpio_chip") or "") != path:
        updates["read_resources"] = []
        updates["config"] = normalize_runtime_config(
            {"reader": {**reader, "gpio_chip": path, "enabled": reader.get("enabled", True)}},
            protocol="gpio",
        )
    if updates:
        repo.update_vm(str(vm["id"]), updates)


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
