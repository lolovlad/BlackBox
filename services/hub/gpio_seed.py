"""Publish the legacy GPIO card and start its VM when a chip is present."""

from __future__ import annotations

from datetime import datetime, timezone

from workers.gpio.pins import DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION, build_gpio_map, normalize_pins, pins_from_fields

from .db import HubRepository
from .vm_config import normalize_runtime_config


def ensure_gpio_vm(repo: HubRepository, *, worker_image: str) -> None:
    if any(str(vm.get("protocol") or "") == "gpio" for vm in repo.list_vms()):
        return
    chips = [item for item in repo.list_resources() if item.get("kind") == "gpio" and item.get("available")]
    if not chips:
        return
    chip = sorted(chips, key=lambda item: str(item.get("path") or item.get("resource_id") or ""))[0]
    repo.approve_resource(str(chip["resource_id"]), None)
    if repo.map_by_version(DEFAULT_GPIO_VERSION, "gpio") is None:
        repo.save_map(build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION))
    path = str(chip.get("path") or "/dev/gpiochip0")
    config = normalize_runtime_config(
        {"reader": {"poll_interval_sec": 0.05, "gpio_chip": path, "enabled": True}},
        protocol="gpio",
    )
    repo.create_vm(
        {
            "name": "GPIO Raspberry",
            "description": "Чтение GPIO Raspberry Pi",
            "protocol": "gpio",
            "preset_id": "gpio-raspberry",
            "map_version": DEFAULT_GPIO_VERSION,
            "read_resources": [
                {
                    "resource_id": chip["resource_id"],
                    "kind": chip["kind"],
                    "name": chip["name"],
                    "path": chip.get("path"),
                    "metadata": chip.get("metadata") or {},
                }
            ],
            "storage_resource_id": "storage:data",
            "config": config,
            "worker_image": worker_image,
            "desired_state": "running",
        }
    )


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
