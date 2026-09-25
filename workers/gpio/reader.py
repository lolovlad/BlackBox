from __future__ import annotations

from typing import Protocol

from workers.gpio.hold import HoldEngine, PinState
from workers.gpio.pins import PinSpec


class PinReader(Protocol):
    def read_pin(self, bcm_pin: int) -> int: ...


def engines_for(pins: list[PinSpec]) -> dict[int, HoldEngine]:
    engines: dict[int, HoldEngine] = {}
    for pin in pins:
        trigger = pin.trigger_level
        if pin.invert:
            trigger = 0 if trigger == 1 else 1
        engines[pin.bcm_pin] = HoldEngine(trigger_level=trigger, hold_sec=pin.hold_sec)
    return engines


def step_pins(
    pins: list[PinSpec],
    backend: PinReader,
    states: dict[int, PinState],
    engines: dict[int, HoldEngine],
    now: float,
) -> list[int]:
    """Return 1 only for pins whose hold timer has latched, matching the legacy ACTIVE list."""
    width = max((pin.address for pin in pins), default=-1) + 1
    flags = [0] * width
    for pin in pins:
        raw = 1 if backend.read_pin(pin.bcm_pin) else 0
        state = states.get(pin.bcm_pin) or PinState(last_value=raw, pending_since=None, alarm_active=False)
        engine = engines[pin.bcm_pin]
        new_state, _opened, _closed = engine.step(now_mono=now, value=raw, state=state)
        states[pin.bcm_pin] = new_state
        if 0 <= pin.address < width:
            flags[pin.address] = 1 if new_state.alarm_active else 0
    return flags
