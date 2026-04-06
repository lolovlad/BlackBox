from __future__ import annotations

import csv
import json
import logging
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from modbus_acquire.deif import (
    ANALOG_CSV_COLUMNS,
    DISCRETE_CSV_COLUMNS,
    analog_discrete_for_csv,
    convert_raw,
    raw_from_registers_and_bits,
)
from modbus_acquire.instrument import build_instrument
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Analogs, Discretes

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


def pack_snapshot(registers: list[int], bits: list[bool]) -> bytes:
    regs = [int(v) & 0xFFFF for v in registers]
    bit_values = [1 if bool(v) else 0 for v in bits]

    reg_part = b"".join(struct.pack(">H", v) for v in regs)
    bit_count = len(bit_values)
    bit_bytes = bytearray((bit_count + 7) // 8)
    for idx, bit in enumerate(bit_values):
        if bit:
            bit_bytes[idx // 8] |= 1 << (idx % 8)

    return struct.pack(">H", len(regs)) + reg_part + struct.pack(">H", bit_count) + bytes(bit_bytes)


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


def decode_to_processed(payload: bytes) -> dict[str, Any]:
    registers, bits = unpack_snapshot(payload)
    raw = raw_from_registers_and_bits(registers, bits)
    return convert_raw(raw)


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
                base = self._config.address_offset - 1
                registers = instrument.read_registers(base + 1, 90)
                bits = instrument.read_bits(base + 16, 32, functioncode=1)
                self._append({"created_at": datetime.now(), "registers": list(registers), "bits": [bool(v) for v in bits]})
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
                blob = pack_snapshot(list(sample.get("registers", [])), list(sample.get("bits", [])))
                session.add(Analogs(created_at=created_at, date=blob))
                session.add(Discretes(created_at=created_at, date=blob))
            session.commit()

            if self._alarms_enabled:
                for sample in batch:
                    created_at = sample["created_at"]
                    raw = raw_from_registers_and_bits(list(sample.get("registers", [])), list(sample.get("bits", [])))
                    active_alarms = convert_raw(raw).get("active_alarms", [])
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


def create_export_csv(session_factory: sessionmaker, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"export_{datetime.now():%Y%m%d_%H%M%S}.csv"
    session = session_factory()
    try:
        rows = session.query(Analogs).order_by(Analogs.created_at.asc()).all()
    finally:
        session.close()

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=",")
        writer.writerow(["line_no", "date", "time", *ANALOG_CSV_COLUMNS, *DISCRETE_CSV_COLUMNS])
        for i, row in enumerate(rows, start=1):
            processed = decode_to_processed(row.date)
            analog, discrete = analog_discrete_for_csv(processed)
            dt = row.created_at
            writer.writerow(
                [
                    i,
                    dt.strftime("%Y-%m-%d"),
                    dt.strftime("%H:%M:%S.%f")[:12],
                    *[analog.get(k, "") for k in ANALOG_CSV_COLUMNS],
                    *[1 if bool(discrete.get(k, False)) else 0 for k in DISCRETE_CSV_COLUMNS],
                ]
            )
    return path
