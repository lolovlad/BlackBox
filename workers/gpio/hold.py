from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PinState:
    last_value: int
    pending_since: float | None
    alarm_active: bool


class HoldEngine:
    """Hold-time filter copied from the legacy Raspberry GPIO reader."""

    def __init__(self, *, trigger_level: int, hold_sec: float) -> None:
        self.trigger_level = int(trigger_level)
        self.hold_sec = float(hold_sec)

    def step(self, *, now_mono: float, value: int, state: PinState) -> tuple[PinState, bool, bool]:
        level = 1 if int(value) else 0
        should_open = False
        should_close = False
        if state.alarm_active:
            if level != self.trigger_level:
                state = PinState(last_value=level, pending_since=None, alarm_active=False)
                should_close = True
            else:
                state = PinState(last_value=level, pending_since=None, alarm_active=True)
            return state, should_open, should_close
        if level == self.trigger_level:
            if self.hold_sec <= 0.0:
                return PinState(last_value=level, pending_since=None, alarm_active=True), True, False
            if state.pending_since is None:
                state = PinState(last_value=level, pending_since=now_mono, alarm_active=False)
            elif now_mono - state.pending_since >= self.hold_sec:
                state = PinState(last_value=level, pending_since=None, alarm_active=True)
                should_open = True
        else:
            state = PinState(last_value=level, pending_since=None, alarm_active=False)
        return state, should_open, should_close
