from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from src.database import AlarmRaspberry
from src.webui.gpio_settings import GpioInputsModel, validate_gpio_inputs_json


class GpioBackend:
    def setup_pin(self, bcm_pin: int, *, pull: str) -> None:  # pragma: no cover (hw)
        raise NotImplementedError

    def read_pin(self, bcm_pin: int) -> int:  # pragma: no cover (hw)
        raise NotImplementedError

    def cleanup(self) -> None:  # pragma: no cover (hw)
        raise NotImplementedError


class RpiGpioBackend(GpioBackend):
    def __init__(self) -> None:  # pragma: no cover (hw)
        import RPi.GPIO as GPIO  # type: ignore

        self._GPIO = GPIO
        try:
            self._GPIO.setwarnings(False)
        except Exception:
            pass
        self._GPIO.setmode(self._GPIO.BCM)

    def setup_pin(self, bcm_pin: int, *, pull: str) -> None:  # pragma: no cover (hw)
        pud = self._GPIO.PUD_OFF
        if pull == "up":
            pud = self._GPIO.PUD_UP
        elif pull == "down":
            pud = self._GPIO.PUD_DOWN
        self._GPIO.setup(int(bcm_pin), self._GPIO.IN, pull_up_down=pud)

    def read_pin(self, bcm_pin: int) -> int:  # pragma: no cover (hw)
        return int(self._GPIO.input(int(bcm_pin)))

    def cleanup(self) -> None:  # pragma: no cover (hw)
        try:
            self._GPIO.cleanup()
        except Exception:
            pass


class GpiodBackend(GpioBackend):
    def __init__(self) -> None:  # pragma: no cover (hw)
        import gpiod  # type: ignore

        self._gpiod = gpiod
        self._chip = gpiod.Chip("gpiochip0")
        self._lines: dict[int, Any] = {}

    def setup_pin(self, bcm_pin: int, *, pull: str) -> None:  # pragma: no cover (hw)
        # libgpiod v2: pulls are configured via line settings; if unavailable, ignore.
        # We keep it best-effort; hardware pull resistors might be configured externally.
        line = self._chip.get_line(int(bcm_pin))
        try:
            # Newer API
            settings = self._gpiod.LineSettings(direction=self._gpiod.line.Direction.INPUT)
            if pull == "up":
                settings.bias = self._gpiod.line.Bias.PULL_UP
            elif pull == "down":
                settings.bias = self._gpiod.line.Bias.PULL_DOWN
            elif pull == "none":
                settings.bias = self._gpiod.line.Bias.DISABLED
            req = self._gpiod.request_lines(
                "/dev/gpiochip0",
                consumer="blackbox-gpio",
                config={int(bcm_pin): settings},
            )
            self._lines[int(bcm_pin)] = req
        except Exception:
            # Fallback: request line without bias control (older bindings / permissions).
            line.request(consumer="blackbox-gpio", type=self._gpiod.LINE_REQ_DIR_IN)
            self._lines[int(bcm_pin)] = line

    def read_pin(self, bcm_pin: int) -> int:  # pragma: no cover (hw)
        obj = self._lines.get(int(bcm_pin))
        if obj is None:
            return 0
        try:
            # request_lines returns a request object with get_value()
            return int(obj.get_value(int(bcm_pin)))
        except Exception:
            return int(obj.get_value())

    def cleanup(self) -> None:  # pragma: no cover (hw)
        try:
            for obj in self._lines.values():
                try:
                    obj.release()
                except Exception:
                    pass
        finally:
            self._lines.clear()
            try:
                self._chip.close()
            except Exception:
                pass


def build_gpio_backend() -> GpioBackend:
    """Prefer gpiod on Pi5; fallback to RPi.GPIO-compatible backend."""
    try:
        return GpiodBackend()
    except Exception:
        return RpiGpioBackend()


def _load_gpio_config(path: Path) -> GpioInputsModel:
    raw = path.read_text(encoding="utf-8")
    cfg_d, err = validate_gpio_inputs_json(raw)
    if err or cfg_d is None:
        raise ValueError(f"Invalid GPIO settings: {err}")
    return GpioInputsModel.model_validate(cfg_d)


@dataclass
class PinState:
    last_value: int
    pending_since: float | None
    alarm_active: bool


class HoldEngine:
    """State machine for 'hold_sec' filtering and alarm open/close."""

    def __init__(self, *, trigger_level: int, hold_sec: float) -> None:
        self.trigger_level = int(trigger_level)
        self.hold_sec = float(hold_sec)

    def step(
        self,
        *,
        now_mono: float,
        value: int,
        state: PinState,
    ) -> tuple[PinState, bool, bool]:
        """Return (new_state, should_open_alarm, should_close_alarm)."""
        v = 1 if int(value) else 0
        should_open = False
        should_close = False

        if state.alarm_active:
            if v != self.trigger_level:
                state = PinState(last_value=v, pending_since=None, alarm_active=False)
                should_close = True
            else:
                state = PinState(last_value=v, pending_since=None, alarm_active=True)
            return state, should_open, should_close

        # not active
        if v == self.trigger_level:
            if self.hold_sec <= 0.0:
                state = PinState(last_value=v, pending_since=None, alarm_active=True)
                should_open = True
                return state, should_open, should_close
            if state.pending_since is None:
                state = PinState(last_value=v, pending_since=now_mono, alarm_active=False)
            else:
                if now_mono - state.pending_since >= self.hold_sec:
                    state = PinState(last_value=v, pending_since=None, alarm_active=True)
                    should_open = True
        else:
            state = PinState(last_value=v, pending_since=None, alarm_active=False)
        return state, should_open, should_close


class GpioCollector:
    def __init__(self, session_factory: sessionmaker, *, gpio_settings_path: Path, backend: GpioBackend) -> None:
        self._session_factory = session_factory
        self._gpio_settings_path = gpio_settings_path
        self._backend = backend
        self._stop = False

        self._cfg = _load_gpio_config(gpio_settings_path)
        self._pins_all = list(self._cfg.pins)
        self._pins_active: list[Any] = []
        self._pin_errors: dict[int, str] = {}
        self._states: dict[int, PinState] = {}
        self._engines: dict[int, HoldEngine] = {}

        for p in self._pins_all:
            pin = int(p.bcm_pin)
            try:
                self._backend.setup_pin(pin, pull=str(p.pull))
                init_val = 1 if self._backend.read_pin(pin) else 0
                self._states[pin] = PinState(last_value=init_val, pending_since=None, alarm_active=False)
                trig = p.trigger_level
                if p.invert:
                    trig = 0 if trig == 1 else 1
                self._engines[pin] = HoldEngine(trigger_level=trig, hold_sec=p.hold_sec)
                self._pins_active.append(p)
            except Exception as exc:
                # Do not crash the subprocess if a pin is busy/unavailable.
                self._pin_errors[pin] = str(exc)

        # Reconcile DB-active pins on startup: close if pin is not active now.
        try:
            self._reconcile_db_active_pins(datetime.now())
        except Exception:
            # Never crash on reconcile; GPIO reading must keep running.
            pass

    @property
    def poll_interval_sec(self) -> float:
        return float(self._cfg.poll_interval_sec)

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        try:
            while not self._stop:
                self.poll_once()
                time.sleep(self.poll_interval_sec)
        finally:
            self._backend.cleanup()

    def poll_once(self) -> None:
        now_mono = time.monotonic()
        now_dt = datetime.now()

        for p in self._pins_active:
            pin = int(p.bcm_pin)
            try:
                val = 1 if self._backend.read_pin(pin) else 0
            except Exception as exc:
                self._pin_errors[pin] = str(exc)
                continue
            st = self._states[pin]
            st2, should_open, should_close = self._engines[pin].step(now_mono=now_mono, value=val, state=st)
            self._states[pin] = st2

            if should_open:
                self._write_alarm(now_dt, p, val, state="active")
                self._states[pin] = PinState(last_value=st2.last_value, pending_since=st2.pending_since, alarm_active=True)
            elif should_close:
                self._write_alarm(now_dt, p, val, state="inactive")
                self._states[pin] = PinState(last_value=st2.last_value, pending_since=st2.pending_since, alarm_active=False)

    def current_pin_values(self) -> list[dict[str, Any]]:
        """Return current pin states for UI (no DB)."""
        out: list[dict[str, Any]] = []
        for p in self._pins_all:
            pin = int(p.bcm_pin)
            st = self._states.get(pin)
            err = self._pin_errors.get(pin)
            if st is None:
                out.append(
                    {
                        "bcm_pin": pin,
                        "name": str(p.name),
                        "value": None,
                        "state": None,
                        "error": err or "unavailable",
                    }
                )
            else:
                is_active = bool(st.alarm_active)
                out.append(
                    {
                        "bcm_pin": pin,
                        "name": str(p.name),
                        "value": int(st.last_value),
                        "state": "active" if is_active else "inactive",
                        "error": err,
                    }
                )
        return out

    def _reconcile_db_active_pins(self, ts: datetime) -> None:
        # Determine which pins are active "right now" by raw level (triggered).
        triggered_now: set[tuple[int, str]] = set()
        for p in self._pins_active:
            pin = int(p.bcm_pin)
            st = self._states.get(pin)
            if st is None:
                continue
            trig = int(p.trigger_level)
            if bool(p.invert):
                trig = 0 if trig == 1 else 1
            if int(st.last_value) == trig:
                triggered_now.add((pin, str(p.name)))

        # Find last known state per (pin,name) and close those which are active in DB but not triggered now.
        session = self._session_factory()
        try:
            rows = (
                session.query(AlarmRaspberry)
                .order_by(AlarmRaspberry.created_at.desc())
                .limit(5000)
                .all()
            )
            last_by_key: dict[tuple[int, str], str] = {}
            for r in rows:
                key = (int(r.bcm_pin), str(r.name))
                if key in last_by_key:
                    continue
                last_by_key[key] = str(getattr(r, "state", ""))

            for key, last_state in last_by_key.items():
                if last_state != "active":
                    continue
                if key in triggered_now:
                    continue
                pin, name = key
                # Write a close event (inactive) on startup.
                self._write_alarm(
                    ts,
                    type("PinCfg", (), {"bcm_pin": pin, "name": name, "trigger_level": 0, "hold_sec": 0.0})(),
                    0,
                    state="inactive",
                    description="startup_reconcile",
                )
        finally:
            session.close()

    def _write_alarm(self, ts: datetime, pin_cfg: Any, value: int, *, state: str, description: str | None = None) -> None:
        session = self._session_factory()
        try:
            row = AlarmRaspberry(
                created_at=ts,
                ended_at=None,
                state=str(state),
                bcm_pin=int(pin_cfg.bcm_pin),
                name=str(pin_cfg.name),
                trigger_level=int(pin_cfg.trigger_level),
                hold_sec=float(pin_cfg.hold_sec),
                description=str(description) if description is not None else f"value={int(value)}",
            )
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

