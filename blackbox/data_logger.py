"""
Главный класс регистратора данных
"""

import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional
import logging
import os
import multiprocessing as mp
import queue

from .config import DataLoggerConfig, AlarmCondition
from .discrete_inputs import DiscreteInputs
from .analog_inputs import AnalogInputs
from .data_writer import DataWriter, AlarmWriter

logger = logging.getLogger(__name__)

_QUEUE_POLL_TIMEOUT_SECONDS = 0.2


def _modbus_process_worker(
    running_flag: "mp.synchronize.Event",
    out_queue: "mp.Queue[Dict[str, object]]",
    reader_config: Dict[str, object],
    poll_interval: float,
) -> None:
    """Воркер-процесс для циклического чтения Modbus."""
    from modbus_acquire.instrument import read_all_data

    while running_flag.is_set():
        try:
            data = read_all_data(reader_config)
            try:
                out_queue.put_nowait(data)
            except queue.Full:
                try:
                    out_queue.get_nowait()
                except queue.Empty:
                    pass
                out_queue.put_nowait(data)
        except Exception:
            # Передаём только факт ошибки, чтобы не падал процесс-читатель.
            try:
                out_queue.put_nowait({"__modbus_process_error__": True})
            except queue.Full:
                pass
        time.sleep(poll_interval)


class DataLogger:
    """Главный класс для регистрации данных"""
    
    def __init__(self, config: Optional[DataLoggerConfig] = None):
        """
        Инициализация регистратора данных
        
        Args:
            config: Конфигурация регистратора (если None, используется конфигурация по умолчанию)
        """
        self.config = config or DataLoggerConfig()
        self.config.validate()

        # Настройка логирования
        self._setup_logging()
        
        # Инициализация компонентов
        self.discrete_inputs = DiscreteInputs(self.config.max_discrete_inputs)
        self.analog_inputs = AnalogInputs(
            self.config.analog_current_inputs,
            self.config.analog_voltage_inputs
        )
        self.data_writer = DataWriter(self.config)
        self.alarm_writer = AlarmWriter(self.config)
        
        # Состояние работы
        self._running = False
        self._analog_thread: Optional[threading.Thread] = None
        self._discrete_thread: Optional[threading.Thread] = None
        self._alarm_thread: Optional[threading.Thread] = None
        self._modbus_thread: Optional[threading.Thread] = None
        self._modbus_read_fn: Optional[Callable[[], Dict[str, object]]] = None
        self._modbus_process: Optional[mp.Process] = None
        self._modbus_process_event: Optional["mp.synchronize.Event"] = None
        self._modbus_data_queue: Optional["mp.Queue[Dict[str, object]]"] = None
        
        # Регистрация callback для дискретных входов
        self._setup_discrete_callbacks()

        logger.info("DataLogger инициализирован (data_directory=%s, alarm_directory=%s)",
                    self.config.data_directory, self.config.alarm_directory)

    def _setup_logging(self) -> None:
        """Настроить систему логирования для библиотеки."""
        os.makedirs(self.config.log_directory, exist_ok=True)

        log_path = os.path.join(self.config.log_directory, "blackbox.log")

        # Не переинициализируем логирование, если уже есть хендлеры
        root_logger = logging.getLogger()
        if not root_logger.handlers:
            handlers = [logging.FileHandler(log_path, encoding="utf-8")]
            if self.config.log_to_console:
                handlers.append(logging.StreamHandler())

            logging.basicConfig(
                level=self.config.log_level,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                handlers=handlers,
            )
        else:
            # Добавляем только файл-логгер для текущего запуска
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(self.config.log_level)
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
    
    def _setup_discrete_callbacks(self):
        """Настроить callbacks для дискретных входов"""
        def on_discrete_change(input_index: int, old_value: bool, new_value: bool):
            """Обработчик изменения дискретного входа"""
            if self._running:
                # Записываем данные при изменении дискретного входа
                self._write_data_point()
        
        for idx in range(self.config.max_discrete_inputs):
            self.discrete_inputs.register_change_callback(idx, on_discrete_change)
    
    def start(self):
        """Запустить регистратор данных"""
        if self._running:
            return
        
        self._running = True
        
        # Запуск потока для опроса аналоговых входов
        self._analog_thread = threading.Thread(target=self._analog_poll_loop, daemon=True)
        self._analog_thread.start()
        
        # Запуск потока для мониторинга аварийных событий
        self._alarm_thread = threading.Thread(target=self._alarm_monitor_loop, daemon=True)
        self._alarm_thread.start()

        # Опциональный поток опроса Modbus
        if self.config.modbus_enabled:
            self._modbus_thread = threading.Thread(target=self._modbus_poll_loop, daemon=True)
            self._modbus_thread.start()
    
    def stop(self):
        """Остановить регистратор данных"""
        self._running = False
        
        # Ждем завершения потоков
        analog_alive = False
        alarm_alive = False
        if self._analog_thread:
            self._analog_thread.join(timeout=2.0)
            analog_alive = self._analog_thread.is_alive()
        if self._alarm_thread:
            self._alarm_thread.join(timeout=2.0)
            alarm_alive = self._alarm_thread.is_alive()
        if self._modbus_thread:
            self._modbus_thread.join(timeout=2.0)
        if self._modbus_process_event:
            self._modbus_process_event.clear()
        if self._modbus_process and self._modbus_process.is_alive():
            self._modbus_process.join(timeout=2.0)
            if self._modbus_process.is_alive():
                self._modbus_process.terminate()
        if self._modbus_data_queue:
            self._modbus_data_queue.close()
            self._modbus_data_queue = None
        self._modbus_process = None
        self._modbus_process_event = None
        
        # Закрываем файлы только если соответствующие потоки завершились.
        # Иначе возможен дедлок на внутренних lock'ах при конкурентной записи.
        if not analog_alive:
            self.data_writer.close()
        if not alarm_alive:
            self.alarm_writer.close()
    
    def _analog_poll_loop(self):
        """Цикл опроса аналоговых входов"""
        while self._running:
            try:
                # Записываем точку данных
                self._write_data_point()
                
                # Добавляем в буфер аварийных событий
                timestamp = datetime.now()
                discrete_values = self.discrete_inputs.get_all_values()
                analog_values = self.analog_inputs.get_all_values()
                self.alarm_writer.add_data_point(timestamp, discrete_values, analog_values)
                
                # Ждем до следующего опроса
                time.sleep(self.config.analog_poll_interval)
            except Exception as e:
                logger.exception("Ошибка в цикле опроса аналоговых входов: %s", e)
                time.sleep(self.config.analog_poll_interval)

    def _modbus_poll_loop(self):
        """Цикл опроса данных с Modbus RTU."""
        # Кастомный reader может быть непиклируемым, для него оставляем потоковый fallback.
        if self._modbus_read_fn is not None:
            while self._running:
                try:
                    data = self._modbus_read_fn()
                    self.update_from_modbus_data(data)
                except Exception as e:
                    logger.exception("Ошибка в цикле опроса Modbus: %s", e)
                time.sleep(self.config.modbus_poll_interval)
            return

        self._modbus_process_event = mp.Event()
        self._modbus_process_event.set()
        self._modbus_data_queue = mp.Queue(maxsize=1)
        self._modbus_process = mp.Process(
            target=_modbus_process_worker,
            args=(
                self._modbus_process_event,
                self._modbus_data_queue,
                self.config.modbus_reader_config,
                self.config.modbus_poll_interval,
            ),
            daemon=True,
        )
        self._modbus_process.start()

        while self._running:
            if self._modbus_data_queue is None:
                break
            try:
                data = self._modbus_data_queue.get(timeout=_QUEUE_POLL_TIMEOUT_SECONDS)
                if data.get("__modbus_process_error__"):
                    logger.warning("Ошибка чтения Modbus в subprocess")
                    continue
                self.update_from_modbus_data(data)
            except queue.Empty:
                continue
            except Exception as e:
                logger.exception("Ошибка в цикле приёма данных Modbus: %s", e)
    
    def _alarm_monitor_loop(self):
        """Цикл мониторинга аварийных событий"""
        active_alarm_name: Optional[str] = None
        active_alarm_start: Optional[datetime] = None
        
        while self._running:
            try:
                discrete_values = self.discrete_inputs.get_all_values()
                analog_values = self.analog_inputs.get_all_values()
                
                # Если авария не активна, проверяем условия
                if not self.alarm_writer.is_alarm_active():
                    # Проверяем все условия аварийных событий
                    for condition in self.config.alarm_conditions:
                        if condition.check(discrete_values, analog_values):
                            # Начинаем запись аварийного события
                            timestamp = datetime.now()
                            self.alarm_writer.start_alarm(condition.name, timestamp)
                            active_alarm_name = condition.name
                            active_alarm_start = timestamp
                            print(f"Аварийное событие: {condition.name}")
                            break
                else:
                    # Авария активна, проверяем время окончания
                    alarm_end_time = self.alarm_writer.get_alarm_end_time()
                    if alarm_end_time and datetime.now() >= alarm_end_time:
                        # Время записи истекло, завершаем
                        if active_alarm_name:
                            self.alarm_writer.finish_alarm(active_alarm_name)
                            print(f"Завершена запись аварийного события: {active_alarm_name}")
                            active_alarm_name = None
                            active_alarm_start = None
                
                time.sleep(0.1)  # Проверка каждые 100мс
            except Exception as e:
                logger.exception("Ошибка в цикле мониторинга аварийных событий: %s", e)
                time.sleep(0.1)
    
    def _write_data_point(self):
        """Записать точку данных"""
        timestamp = datetime.now()
        discrete_values = self.discrete_inputs.get_all_values()
        analog_values = self.analog_inputs.get_all_values()
        self.data_writer.write_data(timestamp, discrete_values, analog_values)

    def update_from_modbus_data(self, data: Dict[str, object]):
        """
        Применить данные, прочитанные по Modbus, к текущему состоянию logger.

        Args:
            data: Результат вызова read_all_data()
        """
        # Аналоговые значения
        for key, idx in self.config.modbus_to_analog_map.items():
            if key not in data:
                continue
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                logger.warning("Некорректное значение Modbus '%s': %s", key, data[key])
                continue

            if idx < self.config.analog_current_inputs:
                self.set_current_value(idx, value)
            else:
                voltage_idx = idx - self.config.analog_current_inputs
                if 0 <= voltage_idx < self.config.analog_voltage_inputs:
                    self.set_voltage_value(voltage_idx, value)

        # Дискретные значения (алармы)
        active_alarms = data.get("alarms", [])
        if isinstance(active_alarms, list):
            active_set = {str(a) for a in active_alarms}
            for alarm_name, discrete_idx in self.config.modbus_alarm_bits_to_discrete_map.items():
                self.set_discrete_value(discrete_idx, alarm_name in active_set)

    def set_modbus_reader(self, reader_fn: Callable[[], Dict[str, object]]):
        """
        Подменить источник Modbus-данных (для кастомной интеграции или тестов).

        Args:
            reader_fn: Функция без аргументов, возвращающая dict данных.
        """
        self._modbus_read_fn = reader_fn
    
    # Методы для работы с дискретными входами (для внешнего скрипта)
    def set_discrete_value(self, input_index: int, value: bool):
        """
        Установить значение дискретного входа
        
        Args:
            input_index: Номер входа (0-19)
            value: Значение (True/False)
        """
        self.discrete_inputs.set_value(input_index, value)
    
    def get_discrete_value(self, input_index: int) -> bool:
        """
        Получить значение дискретного входа
        
        Args:
            input_index: Номер входа
        
        Returns:
            Текущее значение
        """
        return self.discrete_inputs.get_value(input_index)
    
    def get_all_discrete_values(self) -> Dict[int, bool]:
        """Получить все значения дискретных входов"""
        return self.discrete_inputs.get_all_values()
    
    # Методы для работы с аналоговыми входами (для внешнего скрипта)
    def set_current_value(self, input_index: int, value: float):
        """
        Установить значение токового входа
        
        Args:
            input_index: Номер входа (0-2)
            value: Значение тока
        """
        self.analog_inputs.set_current_value(input_index, value)
    
    def set_voltage_value(self, input_index: int, value: float):
        """
        Установить значение входа напряжения генератора
        
        Args:
            input_index: Номер входа (0-2)
            value: Значение напряжения
        """
        self.analog_inputs.set_voltage_value(input_index, value)
    
    def get_current_value(self, input_index: int) -> float:
        """Получить значение токового входа"""
        return self.analog_inputs.get_current_value(input_index)
    
    def get_voltage_value(self, input_index: int) -> float:
        """Получить значение входа напряжения генератора"""
        return self.analog_inputs.get_voltage_value(input_index)
    
    def get_all_analog_values(self) -> Dict[int, float]:
        """Получить все значения аналоговых входов"""
        return self.analog_inputs.get_all_values()
    
    # Методы для настройки аварийных условий
    def add_alarm_condition(self, condition: AlarmCondition):
        """
        Добавить условие аварийного события
        
        Args:
            condition: Условие аварийного события
        """
        self.config.alarm_conditions.append(condition)
    
    def remove_alarm_condition(self, name: str):
        """
        Удалить условие аварийного события
        
        Args:
            name: Имя условия
        """
        self.config.alarm_conditions = [
            c for c in self.config.alarm_conditions if c.name != name
        ]
    
    def get_alarm_conditions(self) -> list[AlarmCondition]:
        """Получить список всех условий аварийных событий"""
        return self.config.alarm_conditions.copy()
    
    def is_running(self) -> bool:
        """Проверить, запущен ли регистратор"""
        return self._running
    
    def __enter__(self):
        """Контекстный менеджер: вход"""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Контекстный менеджер: выход"""
        self.stop()
