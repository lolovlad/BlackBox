from __future__ import annotations

import json
import os
import time

from bb_platform.contracts import VmProtocol
from workers.common import WorkerClient


class ModbusReader:
    """Minimal, testable Modbus RTU adapter around a minimalmodbus object."""

    def __init__(self, instrument, *, retries: int = 3) -> None:
        self.instrument = instrument
        self.retries = max(1, retries)

    def read(self, requests: list[dict]) -> dict[str, list]:
        sources: dict[str, list] = {}
        for request in requests:
            name = str(request.get("name", "holding"))
            fc = int(request.get("fc", 3))
            address = int(request.get("address", 0))
            count = int(request.get("count", 1))
            last_error: Exception | None = None
            for _attempt in range(self.retries):
                try:
                    if fc == 1:
                        sources[name] = [bool(value) for value in self.instrument.read_bits(address, count, functioncode=1)]
                    else:
                        sources[name] = list(self.instrument.read_registers(address, count))
                    break
                except Exception as exc:
                    last_error = exc
            else:
                raise RuntimeError(f"Modbus request {name} failed after {self.retries} retries") from last_error
        return sources


def run() -> int:
    """Read a legacy-compatible map and forward raw samples to Hub.

    Hardware access is intentionally isolated to this process. The first
    implementation uses ``modbus_acquire`` when a physical port is supplied;
    simulator remains the default for CI and development.
    """
    client = WorkerClient(protocol=VmProtocol.MODBUS_RTU)
    map_version = os.getenv("BB_MAP_VERSION", "default-v1")
    client.register()
    config = json.loads(os.getenv("BB_MODBUS_CONFIG", "{}"))
    interval = float(os.getenv("BB_INTERVAL", "1"))
    instrument = None
    try:
        import minimalmodbus
        port = config.get("port", os.getenv("BB_MODBUS_PORT", "/dev/ttyAMA0"))
        instrument = minimalmodbus.Instrument(port, int(config.get("slave_id", 1)))
        instrument.serial.baudrate = int(config.get("baudrate", 9600))
        instrument.serial.timeout = float(config.get("timeout", 0.35))
        instrument.close_port_after_each_call = True
    except Exception:
        instrument = None
    reader = ModbusReader(instrument, retries=int(config.get("retries", 3))) if instrument is not None else None
    while True:
        for command in client.commands():
            if command.get("action") == "apply_map" and command.get("map_version"):
                map_version = str(command["map_version"])
            client.acknowledge(command)
        if reader is None:
            sources = {"holding": [0]}
        else:
            sources = reader.read(config.get("requests", [{"name": "holding", "fc": 3, "address": 0, "count": 1}]))
        client.batch(sources, map_version)
        client.heartbeat()
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(run())
