from __future__ import annotations

import json
import logging
import queue
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import minimalmodbus
from modbus_acquire.instrument import build_instrument
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Emergency, EmergencyConditions, EventLog, Samples
from src.webui.emergency_rule_validation import evaluate_emergency_rule_expression
from src.webui.timezone_utils import now_in_configured_timezone_naive

logger = logging.getLogger(__name__)
SETTINGS_PATH = Path("settings/settings.json")


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


class SettingsCache:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None

    def set_path(self, path: Path) -> None:
        with self._lock:
            self._path = path
            self._cached = None

    def get(self, *, force_reload: bool = False) -> dict[str, Any]:
        with self._lock:
            if force_reload or self._cached is None:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    if not self._path.exists():
                        self._path.write_text("", encoding="utf-8")
                    raw = self._path.read_text(encoding="utf-8")
                    if not raw.strip():
                        self._cached = {"requests": [], "fields": []}
                    else:
                        loaded = json.loads(raw)
                        if isinstance(loaded, dict):
                            self._cached = loaded
                        else:
                            logger.warning("Settings JSON root is not object, using empty config: %s", self._path)
                            self._cached = {"requests": [], "fields": []}
                except Exception:
                    logger.exception("Cannot read settings file, using empty config: %s", self._path)
                    self._cached = {"requests": [], "fields": []}
            if self._cached is None:
                raise RuntimeError("Settings cache is not initialized")
            return self._cached


_SETTINGS_CACHE = SettingsCache(SETTINGS_PATH)


def _load_settings(*, force_reload: bool = False) -> dict[str, Any]:
    return _SETTINGS_CACHE.get(force_reload=force_reload)


def reload_settings_cache() -> dict[str, Any]:
    return _load_settings(force_reload=True)


def configure_settings_path(path: str | Path) -> None:
    p = Path(path)
    resolved = p if p.is_absolute() else (Path.cwd() / p)
    _SETTINGS_CACHE.set_path(resolved)


def _eval_expr(expr: str, context: dict[str, Any]) -> Any:
    # Allow a small, safe subset of helpers for parser expressions.
    safe = {
        "round": round,
        "min": min,
        "max": max,
        "abs": abs,
        "int": int,
        "float": float,
        "bool": bool,
    }
    return eval(expr, {"__builtins__": {}, **safe}, context)


def _uint16_to_int16(raw: int) -> int:
    u = int(raw) & 0xFFFF
    return u - 65536 if u >= 32768 else u


_SNAPSHOT_MAGIC = b"BBX1"


def _pack_bits(bits: list[bool]) -> bytes:
    bit_count = len(bits)
    bit_bytes = bytearray((bit_count + 7) // 8)
    for idx, bit in enumerate(bits):
        if bit:
            bit_bytes[idx // 8] |= 1 << (idx % 8)
    return bytes(bit_bytes)


def _unpack_bits(packed: bytes, bit_count: int) -> list[bool]:
    return [bool((packed[i // 8] >> (i % 8)) & 1) for i in range(bit_count)]


def pack_snapshot(source_values: dict[str, list[Any]]) -> bytes:
    """Pack all Modbus request chunks into a versioned BBX1 blob.

    Stores one segment per request from settings.json (fc=3 or fc=1).
    """
    config = _load_settings()
    segments: list[bytes] = []
    for req in config.get("requests", []):
        name = str(req.get("name") or "")
        if not name:
            continue
        fc = int(req.get("fc", 0))
        values = list(source_values.get(name, []))
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > 255:
            raise ValueError(f"Request name too long for snapshot: {name!r}")

        if fc == 3:
            regs = [int(v) & 0xFFFF for v in values]
            payload = b"".join(struct.pack(">H", v) for v in regs)
            count = len(regs)
        elif fc == 1:
            bits = [bool(v) for v in values]
            payload = _pack_bits(bits)
            count = len(bits)
        else:
            continue

        seg = (
            struct.pack(">B", len(name_bytes))
            + name_bytes
            + struct.pack(">B", fc)
            + struct.pack(">H", count)
            + payload
        )
        segments.append(seg)

    if len(segments) > 0xFFFF:
        raise ValueError("Too many snapshot segments")
    return _SNAPSHOT_MAGIC + struct.pack(">H", len(segments)) + b"".join(segments)


def unpack_snapshot(blob: bytes) -> dict[str, list[Any]]:
    """Unpack BBX1 blob into source_values dict (name -> list of values)."""
    if len(blob) < 6 or blob[:4] != _SNAPSHOT_MAGIC:
        raise ValueError("Not a BBX1 snapshot")
    offset = 4
    seg_count = struct.unpack_from(">H", blob, offset)[0]
    offset += 2
    out: dict[str, list[Any]] = {}
    for _ in range(seg_count):
        if offset >= len(blob):
            raise ValueError("Invalid BBX1 snapshot: truncated segment header")
        name_len = struct.unpack_from(">B", blob, offset)[0]
        offset += 1
        if len(blob) < offset + name_len + 3:
            raise ValueError("Invalid BBX1 snapshot: truncated name/fc/count")
        name = blob[offset : offset + name_len].decode("utf-8", errors="strict")
        offset += name_len
        fc = struct.unpack_from(">B", blob, offset)[0]
        offset += 1
        count = struct.unpack_from(">H", blob, offset)[0]
        offset += 2

        if fc == 3:
            need = count * 2
            if len(blob) < offset + need:
                raise ValueError("Invalid BBX1 snapshot: truncated fc=3 payload")
            regs = [struct.unpack_from(">H", blob, offset + i * 2)[0] for i in range(count)]
            offset += need
            out[name] = regs
        elif fc == 1:
            need = (count + 7) // 8
            if len(blob) < offset + need:
                raise ValueError("Invalid BBX1 snapshot: truncated fc=1 payload")
            packed = blob[offset : offset + need]
            offset += need
            out[name] = _unpack_bits(packed, count)
        else:
            raise ValueError(f"Invalid BBX1 snapshot: unsupported fc={fc}")

    return out


def _unpack_snapshot_legacy(blob: bytes) -> tuple[list[int], list[bool]]:
    """Legacy snapshot format: [u16 reg_count][reg_count*u16][u16 bit_count][bits packed]."""
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
    bits = _unpack_bits(packed, bit_count)
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
        elif f_type in ("int16", "sint16"):
            raw_u = int(values[address]) if address < len(values) else 0
            value = _uint16_to_int16(raw_u)
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


def try_parse_controller_datetime(processed: dict[str, Any], *, field: str = "controller_datetime_iso") -> datetime | None:
    """Parse controller time from processed fields (optional helper).

    Expected format: ISO `YYYY-MM-DDTHH:MM:SS` without timezone.
    """
    raw = processed.get(field)
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.year < 1 or dt.year > 9999:
        return None
    return dt


def _field_categories(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    analog: list[str] = []
    discrete: list[str] = []
    for field in config.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        is_system = bool(field.get("system") or field.get("is_system") or field.get("internal"))
        if is_system:
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
        # New format (BBX1): contains per-request segments.
        if payload[:4] == _SNAPSHOT_MAGIC:
            source_values = unpack_snapshot(payload)
            return parse_fields(config, source_values)

        # Legacy format: one fc=3 block + one fc=1 block.
        registers, bits = _unpack_snapshot_legacy(payload)
        source_values_legacy: dict[str, list[Any]] = {}
        reg_used = False
        bit_used = False
        for req in config.get("requests", []):
            req_name = req.get("name")
            fc = int(req.get("fc", 0))
            if fc == 3 and not reg_used:
                source_values_legacy[req_name] = list(registers)
                reg_used = True
            elif fc == 1 and not bit_used:
                source_values_legacy[req_name] = list(bits)
                bit_used = True
            else:
                source_values_legacy[req_name] = []
        return parse_fields(config, source_values_legacy)
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
        self._thread_lock = threading.Lock()
        self._ram_buffer: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._events_queue: queue.Queue[tuple[datetime, dict[str, Any]]] = queue.Queue(
            maxsize=max(100, self._config.ram_batch_size * 30)
        )
        self._events_stop_event = threading.Event()
        self._events_thread: threading.Thread | None = None
        self._rules_lock = threading.Lock()
        self._rules_snapshot: list[tuple[int, str]] = []
        self._active_alarm_names: set[str] = set()
        self._modbus_data_unavailable = False
        # SQLite: один writer за раз из потоков опроса / emergency — иначе database is locked.
        self._db_write_lock = threading.Lock()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            while not self._events_queue.empty():
                try:
                    self._events_queue.get_nowait()
                    self._events_queue.task_done()
                except queue.Empty:
                    break
            self._rules_snapshot = self._load_active_rules_snapshot()
            self._stop_event.clear()
            self._events_stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._events_thread = threading.Thread(target=self._events_loop, daemon=True)
            self._thread.start()
            self._events_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._events_stop_event.set()
        self.flush_remaining()
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=3)
            if self._events_thread is not None and self._events_thread.is_alive():
                self._events_thread.join(timeout=3)

    def restart(self, new_config: RuntimeConfig | None = None) -> None:
        if new_config is not None:
            self._config = new_config
        reload_settings_cache()
        self.stop()
        self.start()

    def _create_instrument(self):
        return build_instrument(
            {
                "port": self._config.modbus_port,
                "slave_id": self._config.modbus_slave,
                "baudrate": self._config.modbus_baudrate,
                "timeout": self._config.modbus_timeout,
                "clear_buffers_before_each_transaction": True,
                # More robust on noisy/contended links (opens/closes each call).
                "close_port_after_each_call": True,
            }
        )

    def _loop(self) -> None:
        logger.info(
            "Modbus loop started: port=%s slave=%s baud=%s interval=%.3fs",
            self._config.modbus_port,
            self._config.modbus_slave,
            self._config.modbus_baudrate,
            self._config.modbus_interval,
        )
        try:
            instrument = self._create_instrument()
        except Exception:
            logger.exception("Failed to initialize Modbus instrument")
            instrument = None

        consecutive_failures = 0
        last_error_log = 0.0
        last_success_log = 0.0
        while not self._stop_event.is_set():
            try:
                if instrument is None:
                    try:
                        instrument = self._create_instrument()
                        consecutive_failures = 0
                        logger.info("Modbus instrument reinitialized")
                    except Exception:
                        now = time.monotonic()
                        if now - last_error_log >= 3.0:
                            logger.exception("Failed to reinitialize Modbus instrument")
                            last_error_log = now
                    time.sleep(self._config.modbus_interval)
                    continue
                config = _load_settings()
                source_values: dict[str, list[Any]] = {}
                had_error = False
                cycle_read_errors = 0
                requests = list(config.get("requests", []))
                for req in requests:
                    req_name = req["name"]
                    fc = int(req["fc"])
                    address = int(req["address"])
                    count = int(req["count"])
                    last_exc: Exception | None = None
                    for _attempt in range(2):
                        try:
                            if fc == 3:
                                source_values[req_name] = list(instrument.read_registers(address, count))
                            elif fc == 1:
                                source_values[req_name] = [
                                    bool(v) for v in instrument.read_bits(address, count, functioncode=1)
                                ]
                            else:
                                source_values[req_name] = []
                            last_exc = None
                            break
                        except (minimalmodbus.InvalidResponseError, OSError) as exc:
                            last_exc = exc
                            time.sleep(0.02)
                        except Exception as exc:
                            last_exc = exc
                            break
                    if last_exc is not None:
                        had_error = True
                        cycle_read_errors += 1
                        source_values[req_name] = []
                        now = time.monotonic()
                        if now - last_error_log >= 2.0:
                            logger.warning(
                                "Modbus read failed for '%s': %s: %s",
                                req_name,
                                type(last_exc).__name__,
                                last_exc,
                            )
                            last_error_log = now

                # Persist timestamps already in configured APP_TIMEZONE.
                created_at = now_in_configured_timezone_naive()
                processed = parse_fields(config, source_values)
                if self._alarms_enabled:
                    self._persist_alarm_snapshot(created_at=created_at, processed=processed)
                self._append({"created_at": created_at, "sources": source_values, "processed": processed})
                try:
                    self._events_queue.put_nowait((created_at, dict(processed)))
                except queue.Full:
                    logger.warning("Emergency queue is full; dropping snapshot event")
                if had_error:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                no_data = bool(requests) and cycle_read_errors >= len(requests)
                if no_data and not self._modbus_data_unavailable:
                    self._modbus_data_unavailable = True
                    self.flush_remaining()
                    self._log_event(
                        created_at=created_at,
                        level="error",
                        code="modbus_data_unavailable",
                        message="Нет данных Modbus: все запросы в цикле завершились ошибкой.",
                        payload={"cycle_read_errors": cycle_read_errors, "requests_count": len(requests)},
                    )
                    self._close_all_active_alarms(created_at=created_at, reason="modbus_data_unavailable")
                elif (not no_data) and self._modbus_data_unavailable:
                    self._modbus_data_unavailable = False
                    self._log_event(
                        created_at=created_at,
                        level="info",
                        code="modbus_data_restored",
                        message="Данные Modbus восстановлены.",
                        payload={"cycle_read_errors": cycle_read_errors, "requests_count": len(requests)},
                    )
                now = time.monotonic()
                if now - last_success_log >= 5.0:
                    analog_snapshot, _ = analog_discrete_for_csv(processed)
                    # Show a compact health snapshot for operator visibility.
                    logger.info(
                        "Modbus poll: ok=%s cycle_read_errors=%d active_alarms=%d sample={Fgen=%s, Pgen=%s, RPM=%s}",
                        not had_error,
                        cycle_read_errors,
                        len(processed.get("active_alarms", []) or []),
                        analog_snapshot.get("Fgen", "-"),
                        analog_snapshot.get("Pgen", "-"),
                        analog_snapshot.get("RPM", "-"),
                    )
                    last_success_log = now
                if consecutive_failures >= 5:
                    logger.warning("Too many Modbus errors in a row, reinitializing instrument")
                    instrument = None
                    consecutive_failures = 0
            except Exception:
                logger.exception("Unhandled error in Modbus polling loop")
            time.sleep(self._config.modbus_interval)

        logger.info("Modbus loop stopped")

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
        flush_started = time.monotonic()
        with self._db_write_lock:
            session = self._session_factory()
            try:
                for sample in batch:
                    created_at = sample["created_at"]
                    blob = pack_snapshot(dict(sample.get("sources", {})))
                    session.add(Samples(created_at=created_at, date=blob))
                session.commit()
                elapsed_ms = (time.monotonic() - flush_started) * 1000.0
                logger.info(
                    "DB flush: samples_written=%d elapsed_ms=%.1f",
                    len(batch),
                    elapsed_ms,
                )
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

    def _sync_alarm_states(self, *, session, created_at: datetime, processed: dict[str, Any]) -> int:
        active_raw = processed.get("active_alarms", [])
        active_now = {str(v) for v in active_raw} if isinstance(active_raw, list) else set()
        written = 0

        for alarm_name in sorted(active_now):
            is_new = alarm_name not in self._active_alarm_names
            if is_new:
                payload = {"alarm": alarm_name, "created_at": created_at.isoformat(), "state": "active"}
                session.add(
                    Alarms(
                        created_at=created_at,
                        date=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        name=alarm_name,
                        state="active",
                        description="Alarm is active",
                    )
                )
                written += 1
            self._active_alarm_names.add(alarm_name)

        resolved = sorted(self._active_alarm_names - active_now)
        for alarm_name in resolved:
            payload = {"alarm": alarm_name, "created_at": created_at.isoformat(), "state": "inactive"}
            session.add(
                Alarms(
                    created_at=created_at,
                    date=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    name=alarm_name,
                    state="inactive",
                    description="Alarm became inactive",
                )
            )
            written += 1
            self._active_alarm_names.discard(alarm_name)

        return written

    def _persist_alarm_snapshot(self, *, created_at: datetime, processed: dict[str, Any]) -> None:
        with self._db_write_lock:
            session = self._session_factory()
            try:
                written = self._sync_alarm_states(session=session, created_at=created_at, processed=processed)
                if written:
                    session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist alarm states")
            finally:
                session.close()

    def _close_all_active_alarms(self, *, created_at: datetime, reason: str) -> None:
        if not self._active_alarm_names:
            return
        with self._db_write_lock:
            session = self._session_factory()
            try:
                for alarm_name in sorted(self._active_alarm_names):
                    payload = {
                        "alarm": alarm_name,
                        "created_at": created_at.isoformat(),
                        "state": "inactive",
                        "reason": reason,
                    }
                    session.add(
                        Alarms(
                            created_at=created_at,
                            date=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            name=alarm_name,
                            state="inactive",
                            description="Alarm closed due to Modbus data loss",
                        )
                    )
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to close active alarms on Modbus data loss")
            finally:
                session.close()
            self._active_alarm_names.clear()

    def _log_event(
        self,
        *,
        created_at: datetime,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._db_write_lock:
            session = self._session_factory()
            try:
                session.add(
                    EventLog(
                        created_at=created_at,
                        level=str(level),
                        code=str(code),
                        message=str(message),
                        payload_json=json.dumps(payload or {}, ensure_ascii=False),
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to write event log: %s", code)
            finally:
                session.close()

    def _events_loop(self) -> None:
        logger.info("Emergency events worker started")
        while not self._events_stop_event.is_set() or not self._events_queue.empty():
            try:
                created_at, processed = self._events_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._rules_lock:
                    rules_snapshot = list(self._rules_snapshot)
                if rules_snapshot:
                    self._process_rule_events(created_at=created_at, processed=processed, rules_snapshot=rules_snapshot)
            except Exception:
                logger.exception("Emergency events worker failed to process snapshot")
            finally:
                self._events_queue.task_done()
        logger.info("Emergency events worker stopped")

    def _load_active_rules_snapshot(self) -> list[tuple[int, str]]:
        with self._session_factory() as session:
            stmt = (
                select(EmergencyConditions)
                .where(EmergencyConditions.is_deleted.is_(False))
                .order_by(EmergencyConditions.id.asc())
            )
            rows = list(session.execute(stmt).scalars().all())
            return [(row.id, row.condition) for row in rows]

    def _process_rule_events(
        self,
        *,
        created_at: datetime,
        processed: dict[str, Any],
        rules_snapshot: list[tuple[int, str]],
    ) -> None:
        with self._db_write_lock:
            session = self._session_factory()
            changed = False
            try:
                for condition_id, rule_expr in rules_snapshot:
                    ok, fired, err = evaluate_emergency_rule_expression(rule_expr, processed=processed)
                    if not ok:
                        logger.warning("Emergency rule %s evaluation error: %s", condition_id, err)
                        continue
                    if err:
                        logger.info("Emergency rule %s skipped: %s", condition_id, err)
                    if not fired:
                        continue
                    changed = self._upsert_emergency_event(
                        session=session,
                        condition_id=condition_id,
                        fired_at=created_at,
                    ) or changed
                if changed:
                    session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def _upsert_emergency_event(self, *, session, condition_id: int, fired_at: datetime) -> bool:
        stmt = (
            select(Emergency)
            .where(
                Emergency.id_emergency_condition == condition_id,
                Emergency.is_deleted.is_(False),
            )
            .order_by(Emergency.datetime.desc())
            .limit(1)
        )
        last = session.execute(stmt).scalar_one_or_none()
        if last is None:
            session.add(
                Emergency(
                    datetime=fired_at,
                    ended_at=fired_at,
                    id_emergency_condition=condition_id,
                )
            )
            return True
        last_end = last.ended_at or last.datetime
        if fired_at < last.datetime:
            return False
        if fired_at - last_end <= timedelta(minutes=10):
            if last.ended_at is None or fired_at > last.ended_at:
                last.ended_at = fired_at
                return True
            return False
        session.add(
            Emergency(
                datetime=fired_at,
                ended_at=fired_at,
                id_emergency_condition=condition_id,
            )
        )
        return True


