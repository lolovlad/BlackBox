"""
BlackBox - Библиотека для регистрации данных на Raspberry Pi 5
"""

from .data_logger import DataLogger
from .config import DataLoggerConfig, AlarmCondition, DataFormat
from .discrete_inputs import DiscreteInputs
from .analog_inputs import AnalogInputs
from .data_writer import DataWriter, AlarmWriter

try:
    from .modbus_reader import read_all_data
except Exception:  # pragma: no cover - безопасный fallback при отсутствии зависимости
    def read_all_data(*args, **kwargs):
        raise RuntimeError(
            "Modbus-модуль недоступен. Установите зависимость 'minimalmodbus'."
        )

__version__ = "1.0.0"
__all__ = [
    "DataLogger",
    "DataLoggerConfig",
    "AlarmCondition",
    "DataFormat",
    "DiscreteInputs",
    "AnalogInputs",
    "DataWriter",
    "AlarmWriter",
    "read_all_data",
]
