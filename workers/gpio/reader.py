from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from workers.gpio.hold import HoldEngine, PinState
from workers.gpio.pins import PinSpec


class PinReader(Protocol):
    def read_pin(self, bcm_pin: int) -> int | None: ...


@dataclass
class PinFrame:
    active: list[int]
    levels: list[int]
    live: list[int]


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
) -> PinFrame:
    """Read every pin. Active is 1 only after the hold timer latches."""
    width = max((pin.address for pin in pins), default=-1) + 1
    flags = [0] * width
    levels = [0] * width
    live = [0] * width
    for pin in pins:
        if not (0 <= pin.address < width):
            continue
        raw = backend.read_pin(pin.bcm_pin)
        if raw is None:
            continue
        level = 1 if raw else 0
        state = states.get(pin.bcm_pin) or PinState(last_value=level, pending_since=None, alarm_active=False)
        engine = engines[pin.bcm_pin]
        new_state, _opened, _closed = engine.step(now_mono=now, value=level, state=state)
        states[pin.bcm_pin] = new_state
        flags[pin.address] = 1 if new_state.alarm_active else 0
        levels[pin.address] = level
        live[pin.address] = 1
    return PinFrame(active=flags, levels=levels, live=live)
