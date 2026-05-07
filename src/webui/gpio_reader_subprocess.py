from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.webui.gpio_service import GpioCollector, build_gpio_backend


def _write_heartbeat(path: Path, *, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "ts": time.time()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_gpio_state(path: Path, *, pins: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "pins": pins}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    def _log(msg: str) -> None:
        print(msg, flush=True)

    heartbeat_path = Path(os.getenv("GPIO_READER_HEARTBEAT_PATH", "instance/gpio-control/heartbeat.json"))
    stop_path = Path(os.getenv("GPIO_READER_STOP_PATH", "instance/gpio-control/stop.flag"))
    settings_path = Path(os.getenv("GPIO_SETTINGS_PATH", "settings/gpio_inputs.json"))
    state_path = Path(os.getenv("GPIO_READER_STATE_PATH", "instance/gpio-control/state.json"))

    db_path = os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db")
    db_file = Path(db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    backend = build_gpio_backend()
    _log(f"GPIO reader backend={backend.__class__.__name__} settings={settings_path.resolve()}")
    collector = GpioCollector(sf, gpio_settings_path=settings_path.resolve(), backend=backend)
    try:
        pins = collector.current_pin_values()
        ok = sum(1 for p in pins if p.get("value") is not None)
        bad = sum(1 for p in pins if p.get("value") is None)
        _log(f"GPIO reader pins: ok={ok} unavailable={bad}")
        for p in pins:
            if p.get("value") is None:
                _log(f"GPIO pin unavailable: bcm_pin={p.get('bcm_pin')} name={p.get('name')} error={p.get('error')}")
    except Exception as exc:
        _log(f"GPIO reader pin scan failed: {exc}")

    debug = os.getenv("GPIO_READER_DEBUG", "0") == "1"
    last_debug: dict[int, dict] = {}
    last_summary_at = 0.0

    try:
        while True:
            _write_heartbeat(heartbeat_path, pid=os.getpid())
            if stop_path.exists():
                break
            collector.poll_once()  # single step to keep heartbeat loop responsive
            pins_state = collector.current_pin_values()
            _write_gpio_state(state_path, pins=pins_state)

            # Default INFO-like log per poll, similar to Modbus poll log.
            ok = sum(1 for p in pins_state if p.get("value") is not None and not p.get("error"))
            errs = sum(1 for p in pins_state if p.get("error"))
            active = sum(1 for p in pins_state if str(p.get("state")) == "active")
            sample = ", ".join(f"{p.get('name')}={p.get('value')}" for p in pins_state if p.get("value") is not None)
            _log(
                f"GPIO poll: ok={ok} errors={errs} active={active} interval={collector.poll_interval_sec:.3f}s sample={{{{ {sample} }}}}"
            )

            if debug:
                snap = collector.debug_pin_snapshot()
                now = time.time()
                # Summary every 5s
                if now - last_summary_at >= 5.0:
                    last_summary_at = now
                    parts = []
                    for p in snap:
                        parts.append(
                            f"{p.get('bcm_pin')}={p.get('value')} trig={p.get('trigger')} active={p.get('alarm_active')} pending={p.get('pending_sec')}"
                        )
                    _log("GPIO snapshot: " + " | ".join(parts))
                # Change logs
                for p in snap:
                    pin = int(p.get("bcm_pin"))
                    prev = last_debug.get(pin)
                    key = {
                        "value": p.get("value"),
                        "alarm_active": p.get("alarm_active"),
                        "pending_sec": None if p.get("pending_sec") is None else round(float(p.get("pending_sec") or 0.0), 2),
                        "error": p.get("error"),
                    }
                    if prev != key:
                        _log(
                            f"GPIO read: bcm_pin={pin} name={p.get('name')} value={p.get('value')} "
                            f"trigger={p.get('trigger')} hold_sec={p.get('hold_sec')} "
                            f"pending_sec={p.get('pending_sec')} alarm_active={p.get('alarm_active')} error={p.get('error')}"
                        )
                        last_debug[pin] = key
            time.sleep(collector.poll_interval_sec)
    finally:
        collector.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

