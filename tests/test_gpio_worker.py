from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from bb_platform.contracts import MapDocument, RawBatch, RawSample, VmProtocol
from bb_platform.parser import parse_batch
from workers.gpio.hold import PinState
from workers.gpio.pins import DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION, build_gpio_map, pins_from_fields
from workers.gpio.reader import engines_for, step_pins


class _Level:
    def __init__(self, value: int) -> None:
        self.value = value

    def read_pin(self, _bcm_pin: int) -> int:
        return self.value


def test_gpio_hold_is_active_only_after_trigger_duration() -> None:
    document = build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION)
    pins = pins_from_fields(document["fields"])
    assert pins[0].name == "GPIO_27"
    assert pins[0].trigger_level == 0
    backend = _Level(0)
    states: dict[int, PinState] = {}
    engines = engines_for(pins)
    assert step_pins(pins, backend, states, engines, 0.0) == [0]
    assert step_pins(pins, backend, states, engines, 0.4) == [0]
    assert step_pins(pins, backend, states, engines, 0.5) == [1]
    backend.value = 1
    assert step_pins(pins, backend, states, engines, 0.6) == [0]


def test_active_flag_becomes_discrete_for_the_dashboard() -> None:
    document = build_gpio_map(DEFAULT_GPIO_PINS, DEFAULT_GPIO_VERSION)
    pins = pins_from_fields(document["fields"])
    backend = _Level(0)
    states: dict[int, PinState] = {}
    engines = engines_for(pins)
    step_pins(pins, backend, states, engines, 0.0)
    flags = step_pins(pins, backend, states, engines, 1.0)
    vm_id = uuid4()
    batch = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.GPIO,
        map_version=DEFAULT_GPIO_VERSION,
        seq_start=1,
        samples=[RawSample(seq=1, captured_at=datetime.now(timezone.utc), sources={"pins": flags}, quality="good")],
    )
    sample = parse_batch(batch, MapDocument(**document))[0]
    assert sample.discrete["GPIO_27"] is True
    backend.value = 1
    idle = step_pins(pins, backend, states, engines, 2.0)
    idle_batch = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.GPIO,
        map_version=DEFAULT_GPIO_VERSION,
        seq_start=2,
        samples=[RawSample(seq=2, captured_at=datetime.now(timezone.utc), sources={"pins": idle}, quality="good")],
    )
    idle_sample = parse_batch(idle_batch, MapDocument(**document))[0]
    assert idle_sample.discrete["GPIO_27"] is False
