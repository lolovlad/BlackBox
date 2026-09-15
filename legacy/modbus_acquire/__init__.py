"""
Получение данных по Modbus (minimalmodbus).

Отделено от пакета blackbox: для веб-приложения (Flask) подключайте только
`modbus_acquire` + minimalmodbus. Запись в файлы/аварийная логика остаётся в blackbox.
"""

from .instrument import (
    BYTEORDER_ALIASES,
    ModbusFieldSpec,
    ModbusReaderConfig,
    ModbusReader,
    build_instrument,
    read_all_data,
)
from .deif import (
    ALARM_BITS,
    STATUS_BITS,
    ANALOG_CSV_COLUMNS,
    DISCRETE_CSV_COLUMNS,
    analog_discrete_for_csv,
    convert_raw,
    poll_raw,
)

__all__ = [
    "BYTEORDER_ALIASES",
    "ModbusFieldSpec",
    "ModbusReaderConfig",
    "ModbusReader",
    "build_instrument",
    "read_all_data",
    "ALARM_BITS",
    "STATUS_BITS",
    "ANALOG_CSV_COLUMNS",
    "DISCRETE_CSV_COLUMNS",
    "analog_discrete_for_csv",
    "convert_raw",
    "poll_raw",
]
