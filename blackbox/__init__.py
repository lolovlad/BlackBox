"""
BlackBox - Библиотека для регистрации данных на Raspberry Pi 5
"""

from .data_logger import DataLogger
from .config import DataLoggerConfig, AlarmCondition, DataFormat
from .discrete_inputs import DiscreteInputs
from .analog_inputs import AnalogInputs
from .data_writer import DataWriter, AlarmWriter

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
]
