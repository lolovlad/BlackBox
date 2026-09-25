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
    assert [pin.bcm_pin for pin in pins] == list(range(2, 28))
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
    assert list(by_name) == [f"GPIO_{bcm}" for bcm in range(2, 28)]
    assert by_name["GPIO_2"]["is_on"] is False
    assert by_name["GPIO_2"]["level"] == 1
    assert by_name["GPIO_2"]["live"] is True
    assert by_name["GPIO_27"]["is_on"] is True
    assert by_name["GPIO_27"]["live"] is False
    assert by_name["GPIO_27"]["level"] is None
