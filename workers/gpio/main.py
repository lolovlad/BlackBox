from __future__ import annotations

import os
import time

from bb_platform.contracts import VmProtocol
from workers.common import WorkerClient
from workers.gpio.backend import build_gpio_backend
from workers.gpio.pins import pins_from_fields
from workers.gpio.reader import engines_for, step_pins


def run() -> int:
    client = WorkerClient(protocol=VmProtocol.GPIO)
    map_version = os.getenv("BB_MAP_VERSION", "gpio-default-v1")
    client.register()
    backend = None
    pins = []
    states: dict = {}
    engines: dict = {}
    interval = 0.05
    while True:
        try:
            commands = client.commands()
        except Exception:
            time.sleep(1)
            continue
        reload = backend is None
        for command in commands:
            if command.get("action") == "stop":
                if backend is not None:
                    backend.cleanup()
                return 0
            if command.get("action") == "apply_map":
                reload = True
                if command.get("map_version"):
                    map_version = str(command["map_version"])
            client.acknowledge(command)
        if reload:
            try:
                current = client.configuration()
                map_version = str(current.get("map_version") or map_version)
                reader = (current.get("config") or {}).get("reader") or {}
                interval = float(reader.get("poll_interval_sec") or interval)
                chip = str(reader.get("gpio_chip") or "/dev/gpiochip0")
                pins = pins_from_fields((current.get("map") or {}).get("fields") or [])
                if backend is not None:
                    backend.cleanup()
                backend = build_gpio_backend(chip, pins)
                states = {}
                engines = engines_for(pins)
            except Exception as exc:
                backend = None
                try:
                    client.report_error("gpio_open_failed", exc)
                except Exception:
                    pass
                time.sleep(1)
                continue
        flags = step_pins(pins, backend, states, engines, time.monotonic())
        try:
            client.batch({"pins": flags}, map_version)
            client.heartbeat()
        except Exception:
            time.sleep(1)
            continue
        time.sleep(max(0.01, interval))


if __name__ == "__main__":
    raise SystemExit(run())
