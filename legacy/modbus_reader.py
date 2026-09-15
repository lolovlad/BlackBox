"""
Совместимый импорт:
  from modbus_reader import read_all_data
"""

from modbus_acquire.instrument import read_all_data

__all__ = ["read_all_data"]
