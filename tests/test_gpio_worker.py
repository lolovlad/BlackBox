from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from bb_platform.contracts import MapDocument, RawBatch, RawSample, VmProtocol
from bb_platform.parser import parse_batch
from workers.gpio.hold import PinState
from workers.gpio.pins import (
    DEFAULT_GPIO_PINS,
    DEFAULT_GPIO_VERSION,
    HEADER_BCM_PINS,
    build_gpio_map,
    normalize_pins,
    pins_from_fields,
)
from workers.gpio.reader import engines_for, step_pins


class _Level:
    def __init__(self, value: int) -> None:
        self.value = value

    def read_pin(self, _bcm_pin: int) -> int:
        return self.value


def test_gpio_hold_is_active_only_after_trigger_duration() -> None:
    document = build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION)
    pins = pins_from_fields(document["fields"])
    assert [pin.bcm_pin for pin in pins] == list(HEADER_BCM_PINS)
    assert pins[-1].name == "GPIO_27"
    backend = _Level(0)
    states: dict[int, PinState] = {}
    engines = engines_for(pins)
    width = len(pins)
    assert step_pins(pins, backend, states, engines, 0.0).active == [0] * width
    assert step_pins(pins, backend, states, engines, 0.4).active == [0] * width
    assert step_pins(pins, backend, states, engines, 0.5).active == [1] * width
    backend.value = 1
    assert step_pins(pins, backend, states, engines, 0.6).active == [0] * width


def test_missing_line_stays_quiet() -> None:
    pins = pins_from_fields(build_gpio_map([{"bcm_pin": 4, "name": "GPIO_4", "trigger_level": 0, "hold_sec": 0.5, "pull": "up", "invert": False}])["fields"])

    class _Missing:
        def read_pin(self, _bcm_pin: int) -> int | None:
            return None

    frame = step_pins(pins, _Missing(), {}, engines_for(pins), 1.0)
    assert frame.active == [0]
    assert frame.live == [0]
    assert frame.levels == [0]


def test_active_flag_becomes_discrete_for_the_dashboard() -> None:
    document = build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION)
    pins = pins_from_fields(document["fields"])
    backend = _Level(0)
    states: dict[int, PinState] = {}
    engines = engines_for(pins)
    step_pins(pins, backend, states, engines, 0.0)
    frame = step_pins(pins, backend, states, engines, 1.0)
    vm_id = uuid4()
    batch = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.GPIO,
        map_version=DEFAULT_GPIO_VERSION,
        seq_start=1,
        samples=[RawSample(seq=1, captured_at=datetime.now(timezone.utc), sources={"pins": frame.active, "levels": frame.levels, "live": frame.live}, quality="good")],
    )
    sample = parse_batch(batch, MapDocument(**document))[0]
    assert sample.discrete["GPIO_27"] is True
    assert sample.discrete["GPIO_2"] is True
    assert sample.tags["GPIO_27_level"] == 0
    assert sample.tags["GPIO_27_live"] is True
    assert "GPIO_27_level" not in sample.analog
    backend.value = 1
    idle = step_pins(pins, backend, states, engines, 2.0)
    idle_batch = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.GPIO,
        map_version=DEFAULT_GPIO_VERSION,
        seq_start=2,
        samples=[RawSample(seq=2, captured_at=datetime.now(timezone.utc), sources={"pins": idle.active, "levels": idle.levels, "live": idle.live}, quality="good")],
    )
    idle_sample = parse_batch(idle_batch, MapDocument(**document))[0]
    assert idle_sample.discrete["GPIO_27"] is False
    assert idle_sample.tags["GPIO_27_level"] == 1


def test_panel_lists_every_pin_including_quiet_ones() -> None:
    from services.hub.monitor import gpio_panel

    document = build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION)
    vm_id = "panel"

    class _Sample:
        captured_at = datetime.now(timezone.utc)
        discrete = {"GPIO_2": False, "GPIO_27": True}
        tags = {"GPIO_2_level": 1, "GPIO_2_live": True, "GPIO_27_level": 0, "GPIO_27_live": False}

    panel = gpio_panel(
        [{"id": vm_id, "protocol": "gpio", "name": "GPIO Raspberry"}],
        {vm_id: _Sample()},
        {vm_id: document},
    )
    by_name = {item["name"]: item for item in panel["items"]}
    assert list(by_name) == [f"GPIO_{bcm}" for bcm in HEADER_BCM_PINS]
    assert by_name["GPIO_2"]["is_on"] is False
    assert by_name["GPIO_2"]["level"] == 1
    assert by_name["GPIO_2"]["live"] is True
    assert by_name["GPIO_27"]["is_on"] is True
    assert by_name["GPIO_27"]["live"] is False
    assert by_name["GPIO_27"]["level"] is None


def test_uart_pins_are_rejected_and_filtered_from_saved_maps() -> None:
    with pytest.raises(ValueError, match="зарезервирован"):
        normalize_pins(
            [{"bcm_pin": 14, "name": "UART_TX", "trigger_level": 0, "hold_sec": 0.5, "pull": "up"}]
        )
    fields = [
        {
            "bcm_pin": 14,
            "name": "UART_TX",
            "trigger_level": 0,
            "hold_sec": 0.5,
            "pull": "up",
            "address": 0,
        },
        {
            "bcm_pin": 18,
            "name": "GPIO_18",
            "trigger_level": 0,
            "hold_sec": 0.5,
            "pull": "up",
            "address": 1,
        },
    ]
    assert [pin.bcm_pin for pin in pins_from_fields(fields)] == [18]


def _legacy_gpio_panel_with_uart(version: str = "gpio-panel-v2") -> dict:
    """Saved stock map before UART pins were reserved. Cannot go through normalize_pins."""
    pins = [
        {"bcm_pin": bcm, "name": f"GPIO_{bcm}", "trigger_level": 0, "hold_sec": 0.5, "pull": "up", "invert": False}
        for bcm in range(2, 28)
    ]
    fields: list[dict] = []
    for index, pin in enumerate(pins):
        fields.append(
            {
                "name": pin["name"],
                "type": "bool",
                "kind": "discrete",
                "source": "pins",
                "address": index,
                **pin,
            }
        )
        fields.append(
            {
                "name": f"{pin['name']}_level",
                "type": "uint16",
                "kind": "analog",
                "source": "levels",
                "address": index,
                "system": True,
            }
        )
        fields.append(
            {
                "name": f"{pin['name']}_live",
                "type": "bool",
                "kind": "discrete",
                "source": "live",
                "address": index,
                "system": True,
            }
        )
    requests = [
        {"name": "pins", "address": 0, "count": len(pins)},
        {"name": "levels", "address": 0, "count": len(pins)},
        {"name": "live", "address": 0, "count": len(pins)},
    ]
    canonical = {
        "protocol": VmProtocol.GPIO.value,
        "preset_id": "gpio-raspberry",
        "version": version,
        "requests": requests,
        "fields": fields,
    }
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return MapDocument(
        version=version,
        protocol=VmProtocol.GPIO,
        preset_id="gpio-raspberry",
        checksum=checksum,
        requests=requests,
        fields=fields,
    ).model_dump(mode="json")


def test_backend_never_requests_uart_lines(monkeypatch) -> None:
    from workers.gpio.backend import GpiodBackend
    from workers.gpio.pins import PinSpec, UART_RESERVED_BCM_PINS

    requested: list[int] = []
    gpiod = types.ModuleType("gpiod")
    gpiod_line = types.ModuleType("gpiod.line")

    class Bias:
        PULL_UP = "up"
        PULL_DOWN = "down"
        DISABLED = "none"

    class Direction:
        INPUT = "input"

    gpiod_line.Bias = Bias
    gpiod_line.Direction = Direction
    gpiod.line = gpiod_line
    gpiod.LineSettings = lambda **kwargs: kwargs

    def request_lines(chip, consumer, config):
        requested.extend(config)
        return object()

    gpiod.request_lines = request_lines
    monkeypatch.setitem(sys.modules, "gpiod", gpiod)
    monkeypatch.setitem(sys.modules, "gpiod.line", gpiod_line)
    pins = [
        PinSpec(bcm_pin=bcm, name=f"GPIO_{bcm}", trigger_level=0, hold_sec=0.5, pull="up", invert=False, address=index)
        for index, bcm in enumerate([13, 14, 15, 16, 17, 18])
    ]
    GpiodBackend("/dev/gpiochip0", pins)
    assert requested == [13, 18]
    assert not (set(requested) & UART_RESERVED_BCM_PINS)


@pytest.mark.parametrize("version", ["gpio-panel-v1", "gpio-panel-v2"])
def test_seed_replaces_saved_uart_panel_with_new_map(tmp_path: Path, monkeypatch, version: str) -> None:
    from services.hub.db import HubRepository
    from services.hub.gpio_seed import ensure_gpio_vm
    from workers.gpio.pins import DEFAULT_GPIO_PINS, HEADER_BCM_PINS, UART_RESERVED_BCM_PINS

    monkeypatch.setenv("BB_DISCOVERY_GPIO_PATHS", "/dev/gpiochip0")
    repo = HubRepository(tmp_path / "hub.db")
    repo.save_map(_legacy_gpio_panel_with_uart(version))
    vm = repo.create_vm(
        {
            "name": "GPIO Raspberry",
            "protocol": "gpio",
            "preset_id": "gpio-raspberry",
            "map_version": version,
            "read_resources": [],
            "storage_resource_id": "storage:data",
            "worker_image": "blackbox/worker-gpio:dev",
            "config": {
                "reader": {"poll_interval_sec": 0.05, "gpio_chip": "/dev/gpiochip0", "enabled": True},
                "storage": {"target_resource_id": "storage:data"},
            },
            "desired_state": "running",
        }
    )
    old = repo.map_by_version(version, "gpio") or {}
    assert {int(field["bcm_pin"]) for field in old["fields"] if field.get("bcm_pin") is not None} & UART_RESERVED_BCM_PINS
    changed = ensure_gpio_vm(repo, worker_image="blackbox/worker-gpio:dev")
    assert changed == [str(vm["id"])]
    updated = repo.get_vm(str(vm["id"]))
    assert updated is not None
    assert updated["map_version"] == "gpio-panel-v3"
    fresh = repo.map_by_version("gpio-panel-v3", "gpio") or {}
    pins = {int(field["bcm_pin"]) for field in fresh["fields"] if field.get("source") == "pins"}
    assert pins == set(HEADER_BCM_PINS)
    assert not (pins & UART_RESERVED_BCM_PINS)
    assert len(DEFAULT_GPIO_PINS) == len(HEADER_BCM_PINS)
