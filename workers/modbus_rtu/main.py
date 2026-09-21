"""Modbus RTU worker.

The worker owns only the physical serial handle. Maps, poll settings and
storage policy are fetched from Hub, which keeps configuration changes
auditable and lets a running VM receive ``apply-map`` without a new image.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from bb_platform.contracts import VmProtocol
from workers.common import WorkerClient


class ModbusReader:
    """Small, fake-friendly reader for function codes 1--4."""

    def __init__(self, instrument: Any, *, retries: int = 3, retry_delay: float = 0.2, address_offset: int = 1) -> None:
        self.instrument = instrument
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.address_offset = int(address_offset)

    def read(self, requests: list[dict[str, Any]]) -> dict[str, list[Any]]:
        sources: dict[str, list[Any]] = {}
        self.last_failures: list[tuple[str, Exception]] = []
        for request in requests:
            name = str(request.get("name", "holding"))
            fc = int(request.get("fc", 3))
            if fc not in {1, 2, 3, 4}:
                raise ValueError(f"Unsupported Modbus function code: {fc}")
            map_address = int(request.get("address", 0))
            address = map_address + self.address_offset - 1
            count = int(request.get("count", 1))
            max_count = 2000 if fc in {1, 2} else 125
            if address < 0 or address > 0xFFFF or count < 1 or count > max_count or address + count > 0x10000:
                raise ValueError(f"Invalid Modbus request {name}: address/count")
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    if fc in {1, 2}:
                        values = self.instrument.read_bits(address, count, functioncode=fc)
                        sources[name] = [bool(value) for value in values]
                    elif fc in {3, 4}:
                        try:
                            values = self.instrument.read_registers(address, count, functioncode=fc)
                        except TypeError:
                            # Tiny fake instruments used in tests and some
                            # wrappers expose only the FC=3 two-argument API.
                            if fc not in {3, 4}:
                                raise
                            values = self.instrument.read_registers(address, count)
                        sources[name] = [int(value) for value in values]
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.retries:
                        time.sleep(self.retry_delay)
            else:
                sources[name] = []
                if last_error is not None:
                    self.last_failures.append((name, last_error))
        succeeded = [name for name, values in sources.items() if values]
        if self.last_failures and not succeeded:
            name, last_error = self.last_failures[0]
            raise RuntimeError(f"Modbus request {name} failed after {self.retries} retries") from last_error
        return sources


def _reader_config(config: dict[str, Any]) -> dict[str, Any]:
    reader = config.get("reader") if isinstance(config.get("reader"), dict) else config
    return dict(reader or {})


def _make_instrument(config: dict[str, Any]) -> Any:
    import minimalmodbus

    mode_name = str(config.get("mode", "rtu")).lower()
    mode = minimalmodbus.MODE_ASCII if mode_name == "ascii" else minimalmodbus.MODE_RTU
    instrument = minimalmodbus.Instrument(
        str(config.get("port", os.getenv("BB_MODBUS_PORT", "/dev/ttyAMA0"))),
        int(config.get("slave_id", config.get("slave", 1))),
        mode=mode,
    )
    serial = instrument.serial
    serial.baudrate = int(config.get("baudrate", 9600))
    serial.bytesize = int(config.get("bytesize", 8))
    serial.parity = str(config.get("parity", "N")).upper()
    serial.stopbits = float(config.get("stopbits", 1))
    serial.timeout = float(config.get("timeout_sec", config.get("timeout", 0.35)))
    instrument.close_port_after_each_call = bool(config.get("close_port_after_each_call", True))
    instrument.clear_buffers_before_each_transaction = bool(config.get("clear_buffers_before_each_transaction", True))
    return instrument


def _close_instrument(instrument: Any) -> None:
    if instrument is None:
        return
    try:
        serial = getattr(instrument, "serial", None)
        close = getattr(serial, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _fallback_config() -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(os.getenv("BB_PROTOCOL_CONFIG", os.getenv("BB_MODBUS_CONFIG", "{}")))
    map_version = os.getenv("BB_MAP_VERSION", "default-v1")
    requests = raw.get("requests", []) if isinstance(raw, dict) else []
    if isinstance(raw, dict) and isinstance(raw.get("map"), dict):
        requests = raw["map"].get("requests", requests)
    return map_version, raw if isinstance(raw, dict) else {}, list(requests or [])


def run() -> int:
    client = WorkerClient(protocol=VmProtocol.MODBUS_RTU)
    map_version, config, requests = _fallback_config()
    try:
        client.register()
    except Exception as exc:
        try:
            client.report_error("hub_unavailable", exc)
        except Exception:
            pass

    instrument: Any = None
    reader: ModbusReader | None = None
    last_reported_error = ""
    while True:
        try:
            commands = client.commands()
        except Exception as exc:
            if str(exc) != last_reported_error:
                try:
                    client.report_error("hub_unavailable", exc)
                except Exception:
                    pass
                last_reported_error = str(exc)
            time.sleep(1)
            continue
        for command in commands:
            action = str(command.get("action", ""))
            if action == "stop":
                return 0
            if action in {"restart", "apply_map", "apply_config"}:
                try:
                    current = client.configuration()
                    config = dict(current.get("config") or {})
                    map_version = str(current.get("map_version") or map_version)
                    requests = list((current.get("map") or {}).get("requests") or [])
                    reader_cfg = _reader_config(config)
                    _close_instrument(instrument)
                    if reader_cfg.get("enabled", True):
                        instrument = _make_instrument(reader_cfg)
                        reader = ModbusReader(instrument, retries=reader_cfg.get("retries", 3), retry_delay=reader_cfg.get("retry_delay_sec", 0.2), address_offset=reader_cfg.get("address_offset", 1))
                    else:
                        instrument = None
                        reader = None
                    last_reported_error = ""
                    client.acknowledge(command)
                except Exception as exc:
                    client.acknowledge(command, accepted=False, message=str(exc))
                    client.report_error("config_apply_failed", exc)
                continue
            client.acknowledge(command)

        if reader is None:
            try:
                current = client.configuration()
                config = dict(current.get("config") or config)
                map_version = str(current.get("map_version") or map_version)
                requests = list((current.get("map") or {}).get("requests") or requests)
                reader_cfg = _reader_config(config)
                if reader_cfg.get("enabled", True):
                    instrument = _make_instrument(reader_cfg)
                    reader = ModbusReader(instrument, retries=reader_cfg.get("retries", 3), retry_delay=reader_cfg.get("retry_delay_sec", 0.2), address_offset=reader_cfg.get("address_offset", 1))
                else:
                    instrument = None
                    reader = None
            except Exception as exc:
                if str(exc) != last_reported_error:
                    try:
                        client.report_error("reader_unavailable", exc)
                    except Exception:
                        pass
                    last_reported_error = str(exc)
                try:
                    client.heartbeat(health="unhealthy")
                except Exception:
                    pass
                time.sleep(float(_reader_config(config).get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))
                continue

        if reader is None:
            # A deliberately disabled reader still reports health but must not
            # emit synthetic bad samples or touch the physical interface.
            try:
                client.heartbeat()
            except Exception:
                pass
            time.sleep(float(_reader_config(config).get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))
            continue

        try:
            sources = reader.read(requests or [{"name": "holding", "fc": 3, "address": 0, "count": 1}])
            failures = getattr(reader, "last_failures", None) or []
            if failures:
                if str(failures[0][1]) != last_reported_error:
                    try:
                        client.report_error("read_failed", failures[0][1])
                    except Exception:
                        pass
                    last_reported_error = str(failures[0][1])
                client.batch(sources, map_version, quality="degraded")
            else:
                client.batch(sources, map_version)
                last_reported_error = ""
        except Exception as exc:
            if str(exc) != last_reported_error:
                try:
                    client.report_error("read_failed", exc)
                except Exception:
                    pass
                last_reported_error = str(exc)
            try:
                client.batch({}, map_version, quality="bad")
            except Exception:
                pass
            try:
                client.heartbeat(health="unhealthy")
            except Exception:
                pass
            time.sleep(float(_reader_config(config).get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))
            continue

        try:
            client.heartbeat()
        except Exception:
            pass
        time.sleep(float(_reader_config(config).get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))


if __name__ == "__main__":
    raise SystemExit(run())
