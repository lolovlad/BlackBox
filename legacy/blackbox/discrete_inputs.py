"""
Управление дискретными входами
"""

from typing import Dict, Optional, Callable
from datetime import datetime
import threading


class DiscreteInputs:
    """Класс для управления дискретными входами"""
    
    def __init__(self, max_inputs: int = 20):
        """
        Инициализация дискретных входов
        
        Args:
            max_inputs: Максимальное количество входов (до 20)
        """
        if max_inputs < 1 or max_inputs > 20:
            raise ValueError("Количество дискретных входов должно быть от 1 до 20")
        
        self.max_inputs = max_inputs
        self._values: Dict[int, bool] = {}  # Текущие значения
        self._previous_values: Dict[int, bool] = {}  # Предыдущие значения
        self._change_callbacks: Dict[int, Callable[[int, bool, bool], None]] = {}  # Callbacks при изменении
        self._lock = threading.Lock()
    
    def set_value(self, input_index: int, value: bool) -> bool:
        """
        Установить значение дискретного входа
        
        Args:
            input_index: Номер входа (0-19)
            value: Значение (True/False)
        
        Returns:
            True если значение изменилось, False если осталось прежним
        """
        if input_index < 0 or input_index >= self.max_inputs:
            raise ValueError(f"Номер входа должен быть от 0 до {self.max_inputs - 1}")
        
        callback_to_call = None
        args = None
        changed = False
        with self._lock:
            previous = self._values.get(input_index, False)
            self._previous_values[input_index] = previous
            self._values[input_index] = value

            changed = previous != value
            if changed and input_index in self._change_callbacks:
                callback_to_call = self._change_callbacks[input_index]
                args = (input_index, previous, value)

        # Важно: вызываем callback после выхода из lock.
        # Иначе легко получить deadlock, если callback снова обратится к дискретным входам.
        if callback_to_call is not None and args is not None:
            try:
                callback_to_call(*args)
            except Exception as e:
                print(f"Ошибка в callback для входа {input_index}: {e}")

        return changed
    
    def get_value(self, input_index: int) -> bool:
        """
        Получить значение дискретного входа
        
        Args:
            input_index: Номер входа (0-19)
        
        Returns:
            Текущее значение входа
        """
        if input_index < 0 or input_index >= self.max_inputs:
            raise ValueError(f"Номер входа должен быть от 0 до {self.max_inputs - 1}")
        
        with self._lock:
            return self._values.get(input_index, False)
    
    def get_all_values(self) -> Dict[int, bool]:
        """
        Получить все значения дискретных входов
        
        Returns:
            Словарь {номер_входа: значение}
        """
        with self._lock:
            return self._values.copy()
    
    def has_changed(self, input_index: int) -> bool:
        """
        Проверить, изменилось ли значение входа
        
        Args:
            input_index: Номер входа
        
        Returns:
            True если значение изменилось
        """
        if input_index < 0 or input_index >= self.max_inputs:
            return False
        
        with self._lock:
            current = self._values.get(input_index, False)
            previous = self._previous_values.get(input_index, False)
            return current != previous
    
    def get_changed_inputs(self) -> Dict[int, bool]:
        """
        Получить список изменившихся входов
        
        Returns:
            Словарь {номер_входа: новое_значение} для изменившихся входов
        """
        with self._lock:
            changed = {}
            for idx in range(self.max_inputs):
                if self.has_changed(idx):
                    changed[idx] = self._values.get(idx, False)
            return changed
    
    def register_change_callback(self, input_index: int, callback: Callable[[int, bool, bool], None]):
        """
        Зарегистрировать callback при изменении входа
        
        Args:
            input_index: Номер входа
            callback: Функция callback(input_index, old_value, new_value)
        """
        if input_index < 0 or input_index >= self.max_inputs:
            raise ValueError(f"Номер входа должен быть от 0 до {self.max_inputs - 1}")
        
        self._change_callbacks[input_index] = callback
    
    def unregister_change_callback(self, input_index: int):
        """Удалить callback для входа"""
        if input_index in self._change_callbacks:
            del self._change_callbacks[input_index]
    
    def reset(self):
        """Сбросить все значения"""
        with self._lock:
            self._values.clear()
            self._previous_values.clear()
