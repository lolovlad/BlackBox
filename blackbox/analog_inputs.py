"""
Управление аналоговыми входами
"""

from typing import Dict, Optional
import threading


class AnalogInputs:
    """Класс для управления аналоговыми входами"""
    
    def __init__(self, current_inputs: int = 3, voltage_inputs: int = 3):
        """
        Инициализация аналоговых входов
        
        Args:
            current_inputs: Количество токовых входов
            voltage_inputs: Количество входов напряжения генератора
        """
        if current_inputs < 0 or voltage_inputs < 0:
            raise ValueError("Количество входов не может быть отрицательным")
        
        self.current_inputs = current_inputs
        self.voltage_inputs = voltage_inputs
        self._current_values: Dict[int, float] = {}  # Значения токовых входов
        self._voltage_values: Dict[int, float] = {}  # Значения входов напряжения
        self._lock = threading.Lock()
    
    def set_current_value(self, input_index: int, value: float):
        """
        Установить значение токового входа
        
        Args:
            input_index: Номер входа (0, 1, 2)
            value: Значение тока
        """
        if input_index < 0 or input_index >= self.current_inputs:
            raise ValueError(f"Номер токового входа должен быть от 0 до {self.current_inputs - 1}")
        
        with self._lock:
            self._current_values[input_index] = value
    
    def set_voltage_value(self, input_index: int, value: float):
        """
        Установить значение входа напряжения генератора
        
        Args:
            input_index: Номер входа (0, 1, 2)
            value: Значение напряжения
        """
        if input_index < 0 or input_index >= self.voltage_inputs:
            raise ValueError(f"Номер входа напряжения должен быть от 0 до {self.voltage_inputs - 1}")
        
        with self._lock:
            self._voltage_values[input_index] = value
    
    def get_current_value(self, input_index: int) -> float:
        """
        Получить значение токового входа
        
        Args:
            input_index: Номер входа
        
        Returns:
            Текущее значение тока
        """
        if input_index < 0 or input_index >= self.current_inputs:
            raise ValueError(f"Номер токового входа должен быть от 0 до {self.current_inputs - 1}")
        
        with self._lock:
            return self._current_values.get(input_index, 0.0)
    
    def get_voltage_value(self, input_index: int) -> float:
        """
        Получить значение входа напряжения генератора
        
        Args:
            input_index: Номер входа
        
        Returns:
            Текущее значение напряжения
        """
        if input_index < 0 or input_index >= self.voltage_inputs:
            raise ValueError(f"Номер входа напряжения должен быть от 0 до {self.voltage_inputs - 1}")
        
        with self._lock:
            return self._voltage_values.get(input_index, 0.0)
    
    def get_all_current_values(self) -> Dict[int, float]:
        """
        Получить все значения токовых входов
        
        Returns:
            Словарь {номер_входа: значение}
        """
        with self._lock:
            return self._current_values.copy()
    
    def get_all_voltage_values(self) -> Dict[int, float]:
        """
        Получить все значения входов напряжения
        
        Returns:
            Словарь {номер_входа: значение}
        """
        with self._lock:
            return self._voltage_values.copy()
    
    def get_all_values(self) -> Dict[int, float]:
        """
        Получить все аналоговые значения в едином формате
        
        Returns:
            Словарь где ключи: 0-2 для токов, 3-5 для напряжений
        """
        with self._lock:
            all_values = {}
            # Токовые входы: 0, 1, 2
            for idx in range(self.current_inputs):
                all_values[idx] = self._current_values.get(idx, 0.0)
            # Входы напряжения: 3, 4, 5 (смещение на current_inputs)
            for idx in range(self.voltage_inputs):
                all_values[idx + self.current_inputs] = self._voltage_values.get(idx, 0.0)
            return all_values
    
    def reset(self):
        """Сбросить все значения"""
        with self._lock:
            self._current_values.clear()
            self._voltage_values.clear()
