"""
Чтение Modbus RTU через minimalmodbus: декларативные поля, ретраи, сборка Instrument.

Отдельный пакет от BlackBox — для Flask/API достаточно зависимости minimalmodbus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import logging
import time

import minimalmodbus

logger = logging.getLogger(__name__)

BYTEORDER_ALIASES: Dict[str, int] = {
    "big": minimalmodbus.BYTEORDER_BIG,
    "little": minimalmodbus.BYTEORDER_LITTLE,
    "big_swap": minimalmodbus.BYTEORDER_BIG_SWAP,
    "little_swap": minimalmodbus.BYTEORDER_LITTLE_SWAP,
}


@dataclass(frozen=True)
class ModbusFieldSpec:
    """Описание одного поля для чтения."""

    name: str
    address: int
    reg_type: str = "input"  # input | holding
    data_type: str = "u16"  # u16 | s16 | u32 | s32 | bitfield
    scale: float = 1.0
    decimals: Optional[int] = None
    byteorder: int = minimalmodbus.BYTEORDER_BIG
    bit_labels: Dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModbusReaderConfig:
    """Конфигурация Modbus-чтения."""

    port: str = "/dev/ttyAMA0"
    slave_id: int = 1
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 1.0
    mode: str = minimalmodbus.MODE_RTU
    close_port_after_each_call: bool = False
    clear_buffers_before_each_transaction: bool = True
    retry_count: int = 3
    retry_delay_sec: float = 0.2
    include_raw: bool = False
    fields: List[ModbusFieldSpec] = field(default_factory=list)


def _default_fields() -> List[ModbusFieldSpec]:
    return [
        ModbusFieldSpec(name="voltage_L1", address=0, reg_type="input", data_type="u16", scale=0.1),
        ModbusFieldSpec(name="voltage_L2", address=1, reg_type="input", data_type="u16", scale=0.1),
        ModbusFieldSpec(name="voltage_L3", address=2, reg_type="input", data_type="u16", scale=0.1),
        ModbusFieldSpec(name="frequency", address=3, reg_type="input", data_type="u16", scale=0.01),
        ModbusFieldSpec(name="engine_rpm", address=4, reg_type="input", data_type="u16", scale=1.0),
        ModbusFieldSpec(
            name="power",
            address=10,
            reg_type="input",
            data_type="s32",
            scale=0.1,
            byteorder=minimalmodbus.BYTEORDER_BIG,
        ),
        ModbusFieldSpec(
            name="alarms",
            address=20,
            reg_type="input",
            data_type="bitfield",
            bit_labels={
                0: "low_oil_pressure",
                1: "high_coolant_temp",
                2: "overspeed",
                3: "underspeed",
                4: "generator_overvoltage",
                5: "generator_undervoltage",
                6: "low_fuel_level",
                7: "emergency_stop",
            },
        ),
    ]


def _build_default_config() -> ModbusReaderConfig:
    return ModbusReaderConfig(fields=_default_fields())


def _decode_byteorder(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        if key in BYTEORDER_ALIASES:
            return BYTEORDER_ALIASES[key]
    raise ValueError(f"Неподдерживаемый byteorder: {value}")


def _parse_field_spec(field_dict: Dict[str, Any]) -> ModbusFieldSpec:
    byteorder = _decode_byteorder(field_dict.get("byteorder", minimalmodbus.BYTEORDER_BIG))
    labels = field_dict.get("bit_labels", {})
    normalized_labels = {int(k): str(v) for k, v in labels.items()}
    return ModbusFieldSpec(
        name=str(field_dict["name"]),
        address=int(field_dict["address"]),
        reg_type=str(field_dict.get("reg_type", "input")),
        data_type=str(field_dict.get("data_type", "u16")),
        scale=float(field_dict.get("scale", 1.0)),
        decimals=field_dict.get("decimals"),
        byteorder=byteorder,
        bit_labels=normalized_labels,
    )


def _merge_config(config: Optional[Dict[str, Any]]) -> ModbusReaderConfig:
    defaults = _build_default_config()
    if not config:
        return defaults

    fields = defaults.fields
    if "fields" in config:
        fields = [_parse_field_spec(f) for f in config["fields"]]

    return ModbusReaderConfig(
        port=str(config.get("port", defaults.port)),
        slave_id=int(config.get("slave_id", defaults.slave_id)),
        baudrate=int(config.get("baudrate", defaults.baudrate)),
        bytesize=int(config.get("bytesize", defaults.bytesize)),
        parity=str(config.get("parity", defaults.parity)),
        stopbits=int(config.get("stopbits", defaults.stopbits)),
        timeout=float(config.get("timeout", defaults.timeout)),
        mode=str(config.get("mode", defaults.mode)),
        close_port_after_each_call=bool(config.get("close_port_after_each_call", defaults.close_port_after_each_call)),
        clear_buffers_before_each_transaction=bool(
            config.get("clear_buffers_before_each_transaction", defaults.clear_buffers_before_each_transaction)
        ),
        retry_count=int(config.get("retry_count", defaults.retry_count)),
        retry_delay_sec=float(config.get("retry_delay_sec", defaults.retry_delay_sec)),
        include_raw=bool(config.get("include_raw", defaults.include_raw)),
        fields=fields,
    )


def _build_instrument(config: ModbusReaderConfig) -> minimalmodbus.Instrument:
    instrument = minimalmodbus.Instrument(config.port, config.slave_id, mode=config.mode)
    serial = instrument.serial
    serial.baudrate = config.baudrate
    serial.bytesize = config.bytesize
    serial.parity = config.parity
    serial.stopbits = config.stopbits
    serial.timeout = config.timeout
    instrument.close_port_after_each_call = config.close_port_after_each_call
    instrument.clear_buffers_before_each_transaction = config.clear_buffers_before_each_transaction
    return instrument


def _function_code(reg_type: str) -> int:
    if reg_type == "input":
        return 4
    if reg_type == "holding":
        return 3
    raise ValueError(f"Неподдерживаемый тип регистра: {reg_type}")


def _read_16bit(instrument: minimalmodbus.Instrument, field: ModbusFieldSpec, signed: bool) -> int:
    return instrument.read_register(
        registeraddress=field.address,
        number_of_decimals=0,
        functioncode=_function_code(field.reg_type),
        signed=signed,
    )


def _read_32bit(instrument: minimalmodbus.Instrument, field: ModbusFieldSpec, signed: bool) -> int:
    return instrument.read_long(
        registeraddress=field.address,
        functioncode=_function_code(field.reg_type),
        signed=signed,
        byteorder=field.byteorder,
    )


def _apply_scale(raw_value: int, field: ModbusFieldSpec) -> float | int:
    scaled = raw_value * field.scale
    if field.decimals is not None:
        return round(scaled, int(field.decimals))
    if float(field.scale).is_integer():
        return int(scaled)
    return float(scaled)


def _decode_bitfield(mask: int, labels: Dict[int, str]) -> List[str]:
    active: List[str] = []
    for bit, name in sorted(labels.items(), key=lambda item: item[0]):
        if mask & (1 << bit):
            active.append(name)
    return active


def _read_with_retries(read_fn, retries: int, delay_sec: float):
    last_exception: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return read_fn()
        except (IOError, ValueError, minimalmodbus.ModbusException) as exc:
            last_exception = exc
            logger.warning("Ошибка чтения Modbus, попытка %s/%s: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay_sec)
    if last_exception:
        raise last_exception
    raise IOError("Не удалось прочитать Modbus данные")


class ModbusReader:
    """Гибкий reader для чтения полей по декларативной карте."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = _merge_config(config)

    def read_all_data(self) -> Dict[str, Any]:
        instrument = _build_instrument(self.config)
        result: Dict[str, Any] = {}
        raw_data: Dict[str, int] = {}

        for field in self.config.fields:
            if field.data_type in {"u16", "s16", "bitfield"}:
                raw_value = _read_with_retries(
                    lambda f=field: _read_16bit(instrument, f, signed=f.data_type == "s16"),
                    retries=self.config.retry_count,
                    delay_sec=self.config.retry_delay_sec,
                )
            elif field.data_type in {"u32", "s32"}:
                raw_value = _read_with_retries(
                    lambda f=field: _read_32bit(instrument, f, signed=f.data_type == "s32"),
                    retries=self.config.retry_count,
                    delay_sec=self.config.retry_delay_sec,
                )
            else:
                raise ValueError(f"Неподдерживаемый data_type: {field.data_type}")

            raw_data[field.name] = raw_value

            if field.data_type == "bitfield":
                result[field.name] = _decode_bitfield(raw_value, field.bit_labels)
            else:
                result[field.name] = _apply_scale(raw_value, field)

        if self.config.include_raw:
            result["_raw"] = raw_data

        return result


def build_instrument(config: Optional[Dict[str, Any]] = None) -> minimalmodbus.Instrument:
    """Instrument с теми же serial-настройками, что у ModbusReader (для пакетного read_registers/read_bits)."""
    return _build_instrument(_merge_config(config))


def read_all_data(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Чтение всех полей из config['fields'] или набора по умолчанию."""
    return ModbusReader(config=config).read_all_data()


__all__ = [
    "BYTEORDER_ALIASES",
    "ModbusFieldSpec",
    "ModbusReaderConfig",
    "ModbusReader",
    "read_all_data",
    "build_instrument",
]
