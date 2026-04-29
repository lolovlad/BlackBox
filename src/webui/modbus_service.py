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

# Встроенные системные состояния для UI/экспорта.
MODBUS_READING_KEY = "modbus_reading"
MODBUS_ALARM_LABEL = "Чтение по Modbus"


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
    return eval(expr, {"__builtins__": {}}, context)


def _uint16_to_int16(raw: int) -> int:
    u = int(raw) & 0xFFFF
    return u - 65536 if u >= 32768 else u


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

    # Системное дискретное состояние: есть ли фактические данные Modbus в текущем снапшоте.
    has_modbus_requests = bool(config.get("requests"))
    if has_modbus_requests:
        modbus_available = any(isinstance(v, list) and len(v) > 0 for v in source_values.values())
        result[MODBUS_READING_KEY] = bool(modbus_available)

        # В "Сообщения аварий" добавляем одно псевдо-сообщение при пропадании Modbus-данных.
        existing_active_alarms = result.get("active_alarms", [])
        active_list: list[str] = [str(x) for x in existing_active_alarms] if isinstance(existing_active_alarms, list) else []
        if not modbus_available:
            if MODBUS_ALARM_LABEL not in active_list:
                active_list.append(MODBUS_ALARM_LABEL)
        else:
            active_list = [x for x in active_list if x != MODBUS_ALARM_LABEL]
        result["active_alarms"] = active_list
    else:
        # Если Modbus не настроен (нет запросов) — не сигнализируем аварии.
        result[MODBUS_READING_KEY] = True
    return result


def try_parse_controller_datetime(
    processed: dict[str, Any], *, field: str = "controller_datetime_iso"
) -> datetime | None:
    """Пытается преобразовать время контроллера в datetime.

    Ожидаемый формат: ISO `YYYY-MM-DDTHH:MM:SS` без часового пояса.
    """

    raw = processed.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # datetime в Python поддерживает только годы 1..9999; дополнительные проверки на всякий случай.
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
    analog, discrete = _field_categories(_load_settings())
    if MODBUS_READING_KEY not in discrete:
        discrete.append(MODBUS_READING_KEY)
    return analog, discrete


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
                processed = parse_fields(config, source_values)
                app_created_at = now_in_configured_timezone_naive()
                controller_created_at = try_parse_controller_datetime(processed)
                created_at = controller_created_at or app_created_at
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
        buf_len = 0
        with self._lock:
            self._ram_buffer.append(sample)
            buf_len = len(self._ram_buffer)
            if len(self._ram_buffer) >= self._config.ram_batch_size:
                batch = self._ram_buffer[:]
                self._ram_buffer.clear()
        if batch:
            try:
                t0 = batch[0].get("created_at")
                t1 = batch[-1].get("created_at")
                logger.info(
                    "DB flush scheduled: buffer_full size=%d range=%s..%s",
                    len(batch),
                    getattr(t0, "isoformat", lambda: str(t0))(),
                    getattr(t1, "isoformat", lambda: str(t1))(),
                )
            except Exception:
                logger.info("DB flush scheduled: buffer_full size=%d", len(batch))
            self._flush(batch)
        elif buf_len in (1, max(1, self._config.ram_batch_size // 2)):
            # Неболтливый прогресс буфера (1-я запись и 50%).
            logger.info("DB buffer: queued=%d/%d", buf_len, self._config.ram_batch_size)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        flush_started = time.monotonic()
        with self._db_write_lock:
            session = self._session_factory()
            try:
                t0 = batch[0]["created_at"] if batch else None
                t1 = batch[-1]["created_at"] if batch else None
                for sample in batch:
                    created_at = sample["created_at"]
                    blob = pack_snapshot(dict(sample.get("sources", {})))
                    session.add(Samples(created_at=created_at, date=blob))
                session.commit()
                elapsed_ms = (time.monotonic() - flush_started) * 1000.0
                logger.info(
                    "DB flush: samples_written=%d elapsed_ms=%.1f range=%s..%s",
                    len(batch),
                    elapsed_ms,
                    t0.isoformat() if hasattr(t0, "isoformat") else str(t0),
                    t1.isoformat() if hasattr(t1, "isoformat") else str(t1),
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
        # Для UI/экспорта держим системную "Чтение по Modbus" как активную,
        # пока реально нет Modbus-данных. Иначе она может закрыться в тот же тик.
        to_close = {a for a in self._active_alarm_names if a != MODBUS_ALARM_LABEL}
        with self._db_write_lock:
            session = self._session_factory()
            try:
                for alarm_name in sorted(to_close):
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
            # Если системная авария должна оставаться активной — сохраняем её в памяти.
            self._active_alarm_names = self._active_alarm_names.intersection({MODBUS_ALARM_LABEL})

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


