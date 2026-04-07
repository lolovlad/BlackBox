from __future__ import annotations

import json
import logging
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from modbus_acquire.instrument import build_instrument
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Samples

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    db_path: str
    modbus_port: str
    modbus_slave: int
    modbus_baudrate: int
    modbus_timeout: float
    modbus_interval: float
    address_offset: int
    ram_batch_size: int
    static_csv_dir: Path


@lru_cache(maxsize=1)
def _load_settings() -> dict[str, Any]:
    with open("settings/settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _eval_expr(expr: str, context: dict[str, Any]) -> Any:
    return eval(expr, {"__builtins__": {}}, context)


def _pick_sources_for_snapshot(source_values: dict[str, list[Any]]) -> tuple[list[int], list[bool]]:
    config = _load_settings()
    registers: list[int] = []
    bits: list[bool] = []
    for req in config.get("requests", []):
        name = req.get("name")
        fc = int(req.get("fc", 0))
        values = list(source_values.get(name, []))
        if fc == 3 and not registers:
            registers = [int(v) & 0xFFFF for v in values]
        elif fc == 1 and not bits:
            bits = [bool(v) for v in values]
    return registers, bits


def pack_snapshot(source_values: dict[str, list[Any]]) -> bytes:
    registers, bits = _pick_sources_for_snapshot(source_values)
    reg_part = b"".join(struct.pack(">H", v) for v in registers)
    bit_count = len(bits)
    bit_bytes = bytearray((bit_count + 7) // 8)
    for idx, bit in enumerate(bits):
        if bit:
            bit_bytes[idx // 8] |= 1 << (idx % 8)
    return struct.pack(">H", len(registers)) + reg_part + struct.pack(">H", bit_count) + bytes(bit_bytes)


def unpack_snapshot(blob: bytes) -> tuple[list[int], list[bool]]:
    if len(blob) < 4:
        raise ValueError("Snapshot blob is too short")
    offset = 0
    reg_count = struct.unpack_from(">H", blob, offset)[0]
    offset += 2
    reg_bytes_len = reg_count * 2
    if len(blob) < offset + reg_bytes_len + 2:
        raise ValueError("Invalid snapshot blob register section")
    registers = [struct.unpack_from(">H", blob, offset + i * 2)[0] for i in range(reg_count)]
    offset += reg_bytes_len
    bit_count = struct.unpack_from(">H", blob, offset)[0]
    offset += 2
    packed_len = (bit_count + 7) // 8
    if len(blob) < offset + packed_len:
        raise ValueError("Invalid snapshot blob bit section")
    packed = blob[offset : offset + packed_len]
    bits = [bool((packed[i // 8] >> (i % 8)) & 1) for i in range(bit_count)]
    return registers, bits


def parse_fields(config: dict[str, Any], source_values: dict[str, list[Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in config.get("fields", []):
        f_type = field.get("type", "uint16")
        name = field["name"]

        if f_type == "expr":
            try:
                result[name] = _eval_expr(field["expr"], result)
            except Exception:
                result[name] = 0
            continue

        if f_type == "bitfield":
            source = field.get("source")
            address = int(field.get("address", 0))
            values = source_values.get(source, [])
            reg_value = int(values[address]) if address < len(values) else 0
            bits_map = field.get("bits", {})
            active: list[str] = []
            for bit_idx, label in bits_map.items():
                if reg_value & (1 << int(bit_idx)):
                    active.append(str(label))
            result[name] = active
            continue

        source = field.get("source")
        address = int(field.get("address", 0))
        values = source_values.get(source, [])
        if f_type == "uint32_be":
            hi = int(values[address]) if address < len(values) else 0
            lo = int(values[address + 1]) if (address + 1) < len(values) else 0
            value: Any = (hi << 16) | lo
        elif f_type == "bool":
            value = bool(values[address]) if address < len(values) else False
        else:
            value = int(values[address]) if address < len(values) else 0

        if "bit" in field:
            value = bool(int(value) & (1 << int(field["bit"])))

        if "expr" in field:
            try:
                value = _eval_expr(field["expr"], {"x": value, **result})
            except Exception:
                pass
        result[name] = value
    for name in result.get("active_status", []):
        result[name] = True
    return result


def _field_categories(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    analog: list[str] = []
    discrete: list[str] = []
    for field in config.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        f_type = field.get("type", "uint16")
        if f_type == "bool":
            discrete.append(name)
            continue
        if f_type in {"bitfield"} or name in {"active_alarms", "active_status"}:
            continue
        analog.append(name)
    return analog, discrete


def analog_discrete_keys() -> tuple[list[str], list[str]]:
    return _field_categories(_load_settings())


def analog_discrete_for_csv(processed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    analog_keys, discrete_keys = analog_discrete_keys()
    analog = {k: processed.get(k, "") for k in analog_keys}
    discrete = {k: bool(processed.get(k, False)) for k in discrete_keys}
    return analog, discrete


def decode_to_processed(payload: bytes) -> dict[str, Any]:
    config = _load_settings()
    try:
        registers, bits = unpack_snapshot(payload)
        source_values: dict[str, list[Any]] = {}
        reg_used = False
        bit_used = False
        for req in config.get("requests", []):
            req_name = req.get("name")
            fc = int(req.get("fc", 0))
            if fc == 3 and not reg_used:
                source_values[req_name] = list(registers)
                reg_used = True
            elif fc == 1 and not bit_used:
                source_values[req_name] = list(bits)
                bit_used = True
            else:
                source_values[req_name] = []
        return parse_fields(config, source_values)
    except Exception:
        # Backward compatibility for a short transition period if JSON payloads exist.
        try:
            data = json.loads(payload.decode("utf-8"))
            if isinstance(data, dict) and "sources" in data:
                return parse_fields(config, data.get("sources", {}))
        except Exception:
            pass
        return {}


class ModbusCollector:
    def __init__(self, session_factory: sessionmaker, config: RuntimeConfig, alarms_enabled: bool = True) -> None:
        self._session_factory = session_factory
        self._config = config
        self._alarms_enabled = alarms_enabled
        self._lock = threading.Lock()
        self._ram_buffer: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.flush_remaining()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        try:
            instrument = build_instrument(
                {
                    "port": self._config.modbus_port,
                    "slave_id": self._config.modbus_slave,
                    "baudrate": self._config.modbus_baudrate,
                    "timeout": self._config.modbus_timeout,
                    "clear_buffers_before_each_transaction": True,
                    "close_port_after_each_call": False,
                }
            )
        except Exception:
            logger.exception("Failed to initialize Modbus instrument")
            instrument = None

        while not self._stop_event.is_set():
            try:
                if instrument is None:
                    time.sleep(self._config.modbus_interval)
                    continue
                config = _load_settings()
                source_values: dict[str, list[Any]] = {}
                for req in config.get("requests", []):
                    req_name = req["name"]
                    fc = int(req["fc"])
                    address = int(req["address"])
                    count = int(req["count"])
                    if fc == 3:
                        source_values[req_name] = list(instrument.read_registers(address, count))
                    elif fc == 1:
                        source_values[req_name] = [bool(v) for v in instrument.read_bits(address, count, functioncode=1)]
                    else:
                        source_values[req_name] = []
                self._append({"created_at": datetime.now(), "sources": source_values})
            except Exception:
                logger.exception("Unhandled error in Modbus polling loop")
            time.sleep(self._config.modbus_interval)

    def _append(self, sample: dict[str, Any]) -> None:
        batch: list[dict[str, Any]] = []
        with self._lock:
            self._ram_buffer.append(sample)
            if len(self._ram_buffer) >= self._config.ram_batch_size:
                batch = self._ram_buffer[:]
                self._ram_buffer.clear()
        if batch:
            self._flush(batch)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        session = self._session_factory()
        try:
            for sample in batch:
                created_at = sample["created_at"]
                blob = pack_snapshot(dict(sample.get("sources", {})))
                session.add(Samples(created_at=created_at, date=blob))
            session.commit()

            if self._alarms_enabled:
                for sample in batch:
                    created_at = sample["created_at"]
                    active_alarms = parse_fields(_load_settings(), dict(sample.get("sources", {}))).get("active_alarms", [])
                    if not isinstance(active_alarms, list):
                        continue
                    for alarm_name in active_alarms:
                        payload = {"alarm": alarm_name, "created_at": created_at.isoformat()}
                        session.add(
                            Alarms(
                                created_at=created_at,
                                date=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                name=str(alarm_name),
                                description="Alarm from converted Modbus data",
                            )
                        )
                session.commit()
        except OperationalError:
            session.rollback()
            logger.exception("Database flush failed. Run migrations: flask db upgrade")
        except Exception:
            session.rollback()
            logger.exception("Database flush failed for %d samples", len(batch))
        finally:
            session.close()

    def flush_remaining(self) -> None:
        with self._lock:
            batch = self._ram_buffer[:]
            self._ram_buffer.clear()
        if batch:
            self._flush(batch)


