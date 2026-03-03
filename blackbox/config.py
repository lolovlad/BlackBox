"""
Конфигурация для регистратора данных
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Any
from enum import Enum
from datetime import datetime
import logging


class DataFormat(Enum):
    """Форматы данных для записи"""
    CSV = "csv"
    JSON = "json"
    BINARY = "binary"


@dataclass
class AlarmCondition:
    """Условие для аварийного события"""
    name: str
    discrete_inputs: Optional[List[int]] = None  # Номера дискретных входов для мониторинга
    analog_inputs: Optional[List[int]] = None  # Номера аналоговых входов для мониторинга
    discrete_condition: Optional[Callable[[Dict[int, bool]], bool]] = None  # Функция проверки дискретов
    analog_condition: Optional[Callable[[Dict[int, float]], bool]] = None  # Функция проверки аналогов
    threshold_min: Optional[float] = None  # Минимальное значение для аналогов
    threshold_max: Optional[float] = None  # Максимальное значение для аналогов
    
    def check(self, discrete_values: Dict[int, bool], analog_values: Dict[int, float]) -> bool:
        """Проверка условия аварийного события"""
        result = True
        
        # Проверка дискретных входов
        if self.discrete_inputs is not None and self.discrete_condition is not None:
            discrete_dict = {idx: discrete_values.get(idx, False) for idx in self.discrete_inputs}
            result = result and self.discrete_condition(discrete_dict)
        
        # Проверка аналоговых входов
        if self.analog_inputs is not None and self.analog_condition is not None:
            analog_dict = {idx: analog_values.get(idx, 0.0) for idx in self.analog_inputs}
            result = result and self.analog_condition(analog_dict)
        
        # Проверка пороговых значений
        if self.analog_inputs is not None:
            for idx in self.analog_inputs:
                value = analog_values.get(idx, 0.0)
                if self.threshold_min is not None and value < self.threshold_min:
                    result = result and False
                if self.threshold_max is not None and value > self.threshold_max:
                    result = result and False
        
        return result


@dataclass
class DataLoggerConfig:
    """Конфигурация регистратора данных"""
    # Пути для записи
    data_directory: str = "./data"  # Основная директория для данных
    alarm_directory: str = "./alarms"  # Директория для аварийных событий
    backup_directory: str = "./backup"  # Директория для резервного хранения временных данных
    log_directory: str = "./logs"  # Директория для логов
    
    # Настройки входов
    max_discrete_inputs: int = 20  # Максимальное количество дискретных входов
    analog_current_inputs: int = 3  # Количество токовых входов
    analog_voltage_inputs: int = 3  # Количество входов напряжения генератора
    
    # Настройки опроса
    analog_poll_interval: float = 0.1  # Интервал опроса аналоговых входов (сек)
    discrete_poll_on_change: bool = True  # Опрос дискретных входов по изменению
    
    # Настройки записи
    data_format: DataFormat = DataFormat.CSV
    overwrite_data: bool = True  # Возможность перезаписи обычных данных
    overwrite_alarms: bool = False  # Перезапись аварийных данных запрещена
    fsync_on_write: bool = True  # Гарантировать сброс данных на диск после записи
    
    # Настройки аварийных событий
    alarm_conditions: List[AlarmCondition] = field(default_factory=list)
    alarm_pre_time: int = 300  # Время записи до события (сек) - 5 минут
    alarm_post_time: int = 900  # Время записи после события (сек) - 15 минут

    # Настройки резервного хранения
    enable_backup_storage: bool = True  # Включить резервное хранение при ошибках записи
    
    # Настройки формата данных
    csv_delimiter: str = ","
    csv_include_timestamp: bool = True
    csv_include_discrete: bool = True
    csv_include_analog: bool = True

    # Настройки логирования
    log_level: int = logging.INFO  # Уровень логирования
    log_to_console: bool = True  # Дублировать логи в консоль
    
    # Порядок колонок в CSV (гибкая настройка)
    csv_column_order: List[str] = field(default_factory=lambda: [
        "timestamp",
        "discrete_0", "discrete_1", "discrete_2", "discrete_3", "discrete_4",
        "discrete_5", "discrete_6", "discrete_7", "discrete_8", "discrete_9",
        "discrete_10", "discrete_11", "discrete_12", "discrete_13", "discrete_14",
        "discrete_15", "discrete_16", "discrete_17", "discrete_18", "discrete_19",
        "current_0", "current_1", "current_2",
        "voltage_0", "voltage_1", "voltage_2"
    ])
    
    # Имена колонок (для кастомизации)
    csv_column_names: Dict[str, str] = field(default_factory=lambda: {})
    
    def get_column_name(self, column_key: str) -> str:
        """Получить имя колонки (с учетом кастомизации)"""
        return self.csv_column_names.get(column_key, column_key)
    
    def validate(self) -> bool:
        """Валидация конфигурации"""
        if self.max_discrete_inputs < 1 or self.max_discrete_inputs > 20:
            raise ValueError("max_discrete_inputs должен быть от 1 до 20")
        if self.analog_poll_interval <= 0:
            raise ValueError("analog_poll_interval должен быть больше 0")
        if self.alarm_pre_time < 0 or self.alarm_post_time < 0:
            raise ValueError("Время записи аварийных событий не может быть отрицательным")
        return True
