"""
Классы для записи данных
"""

import os
import csv
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
import threading
from collections import deque
import logging

from .config import DataFormat, DataLoggerConfig

logger = logging.getLogger(__name__)


class DataWriter:
    """Класс для записи обычных данных"""

    def __init__(self, config: DataLoggerConfig):
        """
        Инициализация записи данных

        Args:
            config: Конфигурация регистратора
        """
        self.config = config
        self._current_file = None
        self._current_file_path: Optional[Path] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()
        # Резервное хранение
        self._backup_queue: deque[Dict[str, Any]] = deque()
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Создать необходимые директории"""
        os.makedirs(self.config.data_directory, exist_ok=True)
        if self.config.enable_backup_storage:
            os.makedirs(self.config.backup_directory, exist_ok=True)
    
    def _get_date_string(self) -> str:
        """Получить строку с текущей датой"""
        return datetime.now().strftime("%Y-%m-%d")
    
    def _get_file_path(self, date_str: str) -> Path:
        """Получить путь к файлу для указанной даты"""
        date_dir = Path(self.config.data_directory) / date_str
        date_dir.mkdir(exist_ok=True)
        return date_dir / f"data.{self.config.data_format.value}"
    
    def _open_file(self, file_path: Path):
        """Открыть файл для записи"""
        if self._current_file:
            self._current_file.close()
        
        # Проверка перезаписи
        if file_path.exists() and not self.config.overwrite_data:
            # Добавляем суффикс если файл существует
            counter = 1
            while file_path.exists():
                new_name = f"data_{counter}.{self.config.data_format.value}"
                file_path = file_path.parent / new_name
                counter += 1
        
        self._current_file_path = file_path
        try:
            self._current_file = open(file_path, 'a', newline='', encoding='utf-8')
        except OSError as exc:
            logger.error("Не удалось открыть файл данных %s: %s", file_path, exc)
            self._current_file = None
            return

        # Записать заголовок если файл новый
        try:
            if file_path.stat().st_size == 0:
                self._write_header()
        except OSError as exc:
            logger.error("Не удалось прочитать размер файла %s: %s", file_path, exc)
    
    def _write_header(self):
        """Записать заголовок файла"""
        if self.config.data_format == DataFormat.CSV:
            writer = csv.writer(self._current_file, delimiter=self.config.csv_delimiter)
            headers = [self.config.get_column_name(col) for col in self.config.csv_column_order]
            writer.writerow(headers)
    
    def _check_date_change(self):
        """Проверить изменение даты и переключить файл при необходимости"""
        current_date = self._get_date_string()
        
        if self._current_date != current_date:
            self._current_date = current_date
            file_path = self._get_file_path(current_date)
            self._open_file(file_path)
    
    def write_data(self, timestamp: datetime, discrete_values: Dict[int, bool], 
                   analog_values: Dict[int, float]):
        """
        Записать данные
        
        Args:
            timestamp: Временная метка
            discrete_values: Значения дискретных входов
            analog_values: Значения аналоговых входов
        """
        record = {
            "timestamp": timestamp,
            "discrete": discrete_values.copy(),
            "analog": analog_values.copy(),
        }

        with self._lock:
            self._check_date_change()

            if not self._current_file:
                # Если основной файл недоступен — сохраняем в резервную очередь
                logger.warning("Основной файл данных недоступен, запись в резервную очередь")
                if self.config.enable_backup_storage:
                    self._backup_queue.append(record)
                    self._write_backup_record(record)
                return

            try:
                if self.config.data_format == DataFormat.CSV:
                    self._write_csv_row(timestamp, discrete_values, analog_values)
                elif self.config.data_format == DataFormat.JSON:
                    self._write_json_row(timestamp, discrete_values, analog_values)

                # При успешной записи пробуем выгрузить резервные данные
                if self.config.enable_backup_storage and self._backup_queue:
                    logger.info("Пробуем выгрузить %d резервных записей", len(self._backup_queue))
                    while self._backup_queue:
                        backup_record = self._backup_queue.popleft()
                        self._write_from_backup_record(backup_record)

            except OSError as exc:
                logger.error("Ошибка при записи данных в основной файл %s: %s", self._current_file_path, exc)
                if self.config.enable_backup_storage:
                    self._backup_queue.append(record)
                    self._write_backup_record(record)
    
    def _write_csv_row(self, timestamp: datetime, discrete_values: Dict[int, bool],
                      analog_values: Dict[int, float]):
        """Записать строку в CSV"""
        if not self._current_file:
            raise OSError("Основной файл данных не открыт")

        writer = csv.writer(self._current_file, delimiter=self.config.csv_delimiter)
        row = []

        for col in self.config.csv_column_order:
            if col == "timestamp":
                row.append(timestamp.isoformat())
            elif col.startswith("discrete_"):
                idx = int(col.split("_")[1])
                row.append(1 if discrete_values.get(idx, False) else 0)
            elif col.startswith("current_"):
                idx = int(col.split("_")[1])
                row.append(analog_values.get(idx, 0.0))
            elif col.startswith("voltage_"):
                idx = int(col.split("_")[1])
                # Напряжения хранятся со смещением
                row.append(analog_values.get(idx + self.config.analog_current_inputs, 0.0))
            else:
                row.append("")

        writer.writerow(row)
        self._current_file.flush()
        if self.config.fsync_on_write:
            os.fsync(self._current_file.fileno())
    
    def _write_json_row(self, timestamp: datetime, discrete_values: Dict[int, bool],
                       analog_values: Dict[int, float]):
        """Записать строку в JSON"""
        if not self._current_file:
            raise OSError("Основной файл данных не открыт")

        data = {
            "timestamp": timestamp.isoformat(),
            "discrete": {f"input_{k}": v for k, v in discrete_values.items()},
            "analog": {
                "current": {f"input_{k}": v for k, v in analog_values.items() 
                           if k < self.config.analog_current_inputs},
                "voltage": {f"input_{k}": v for k, v in analog_values.items() 
                           if k >= self.config.analog_current_inputs}
            }
        }
        json.dump(data, self._current_file, ensure_ascii=False)
        self._current_file.write("\n")
        self._current_file.flush()
        if self.config.fsync_on_write:
            os.fsync(self._current_file.fileno())

    # ---------- Резервное хранение ----------

    def _backup_file_path(self) -> Path:
        """Путь к файлу резервного хранения."""
        return Path(self.config.backup_directory) / "backup_data.jsonl"

    def _write_backup_record(self, record: Dict[str, Any]) -> None:
        """Записать запись в резервный файл (json-lines)."""
        if not self.config.enable_backup_storage:
            return

        path = self._backup_file_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                serializable = {
                    "timestamp": record["timestamp"].isoformat(),
                    "discrete": record["discrete"],
                    "analog": record["analog"],
                }
                json.dump(serializable, f, ensure_ascii=False)
                f.write("\n")
            logger.warning("Записана резервная запись в %s", path)
        except OSError as exc:
            logger.error("Не удалось записать резервную запись в %s: %s", path, exc)

    def _write_from_backup_record(self, record: Dict[str, Any]) -> None:
        """Повторная запись резервной записи в основной файл."""
        ts: datetime = record["timestamp"]
        discrete: Dict[int, bool] = record["discrete"]
        analog: Dict[int, float] = record["analog"]

        if self.config.data_format == DataFormat.CSV:
            self._write_csv_row(ts, discrete, analog)
        elif self.config.data_format == DataFormat.JSON:
            self._write_json_row(ts, discrete, analog)
    
    def close(self):
        """Закрыть файл"""
        # Закрытие должно быть безопасным при конкурентной записи.
        # Если другой поток держит lock — не ждём бесконечно.
        if not self._lock.acquire(timeout=0.2):
            return
        try:
            if self._current_file:
                self._current_file.close()
                self._current_file = None
        finally:
            self._lock.release()


class AlarmWriter:
    """Класс для записи аварийных событий"""
    
    def __init__(self, config: DataLoggerConfig):
        """
        Инициализация записи аварийных событий
        
        Args:
            config: Конфигурация регистратора
        """
        self.config = config
        self._buffer: deque = deque(maxlen=int((config.alarm_pre_time + config.alarm_post_time) / 
                                               config.analog_poll_interval) + 100)
        self._alarm_active = False
        self._alarm_start_time: Optional[datetime] = None
        self._alarm_end_time: Optional[datetime] = None
        self._lock = threading.Lock()
        # Резервное хранение
        self._backup_queue: deque[Dict[str, Any]] = deque()
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Создать необходимые директории"""
        os.makedirs(self.config.alarm_directory, exist_ok=True)
        if self.config.enable_backup_storage:
            os.makedirs(self.config.backup_directory, exist_ok=True)
    
    def add_data_point(self, timestamp: datetime, discrete_values: Dict[int, bool],
                      analog_values: Dict[int, float]):
        """
        Добавить точку данных в буфер
        
        Args:
            timestamp: Временная метка
            discrete_values: Значения дискретных входов
            analog_values: Значения аналоговых входов
        """
        with self._lock:
            self._buffer.append({
                "timestamp": timestamp,
                "discrete": discrete_values.copy(),
                "analog": analog_values.copy()
            })
    
    def start_alarm(self, alarm_name: str, timestamp: datetime):
        """
        Начать запись аварийного события
        
        Args:
            alarm_name: Имя аварийного события
            timestamp: Время начала события
        """
        with self._lock:
            self._alarm_active = True
            self._alarm_start_time = timestamp
            # Вычисляем время окончания записи
            self._alarm_end_time = timestamp + \
                timedelta(seconds=self.config.alarm_post_time)
    
    def is_alarm_active(self) -> bool:
        """Проверить, активно ли аварийное событие"""
        with self._lock:
            # Время окончания обрабатывается снаружи (DataLogger._alarm_monitor_loop),
            # чтобы избежать ситуации, когда событие "схлопывается" раньше, чем finish_alarm().
            return bool(self._alarm_active)
    
    def get_alarm_end_time(self) -> Optional[datetime]:
        """Получить время окончания записи аварийного события"""
        with self._lock:
            return self._alarm_end_time
    
    def finish_alarm(self, alarm_name: str):
        """
        Завершить запись аварийного события и сохранить файл
        
        Args:
            alarm_name: Имя аварийного события
        """
        with self._lock:
            if not self._alarm_active:
                return
            
            # Сохраняем данные
            self._save_alarm_data(alarm_name)
            
            # Очищаем буфер
            self._buffer.clear()
            self._alarm_active = False
            self._alarm_start_time = None
            self._alarm_end_time = None
    
    def _save_alarm_data(self, alarm_name: str):
        """Сохранить данные аварийного события"""
        if not self._alarm_start_time:
            return
        
        # Создаем имя файла с временной меткой
        timestamp_str = self._alarm_start_time.strftime("%Y%m%d_%H%M%S")
        safe_alarm_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in alarm_name)
        filename = f"alarm_{safe_alarm_name}_{timestamp_str}.{self.config.data_format.value}"
        
        # Проверяем уникальность имени файла (перезапись запрещена)
        file_path = Path(self.config.alarm_directory) / filename
        counter = 1
        while file_path.exists():
            filename = f"alarm_{safe_alarm_name}_{timestamp_str}_{counter}.{self.config.data_format.value}"
            file_path = Path(self.config.alarm_directory) / filename
            counter += 1
        
        # Фильтруем данные: берем 5 минут до и 15 минут после
        pre_time = self._alarm_start_time - timedelta(seconds=self.config.alarm_pre_time)
        
        filtered_data = []
        for data_point in self._buffer:
            if pre_time <= data_point["timestamp"] <= self._alarm_end_time:
                filtered_data.append(data_point)
        
        # Записываем данные
        if self.config.data_format == DataFormat.CSV:
            self._write_alarm_csv(file_path, filtered_data)
        elif self.config.data_format == DataFormat.JSON:
            self._write_alarm_json(file_path, filtered_data)
    
    def _write_alarm_csv(self, file_path: Path, data: List[Dict]):
        """Записать аварийные данные в CSV"""
        try:
            with open(file_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=self.config.csv_delimiter)

                # Заголовок
                headers = [self.config.get_column_name(col) for col in self.config.csv_column_order]
                writer.writerow(headers)

                # Данные
                for data_point in data:
                    row = []
                    discrete_values = data_point["discrete"]
                    analog_values = data_point["analog"]

                    for col in self.config.csv_column_order:
                        if col == "timestamp":
                            row.append(data_point["timestamp"].isoformat())
                        elif col.startswith("discrete_"):
                            idx = int(col.split("_")[1])
                            row.append(1 if discrete_values.get(idx, False) else 0)
                        elif col.startswith("current_"):
                            idx = int(col.split("_")[1])
                            row.append(analog_values.get(idx, 0.0))
                        elif col.startswith("voltage_"):
                            idx = int(col.split("_")[1])
                            row.append(analog_values.get(idx + self.config.analog_current_inputs, 0.0))
                        else:
                            row.append("")

                    writer.writerow(row)
                if self.config.fsync_on_write:
                    os.fsync(f.fileno())
        except OSError as exc:
            logger.error("Ошибка при записи аварийного CSV файла %s: %s", file_path, exc)
            if self.config.enable_backup_storage:
                for dp in data:
                    self._backup_queue.append(dp)
                self._write_alarm_backup()
    
    def _write_alarm_json(self, file_path: Path, data: List[Dict]):
        """Записать аварийные данные в JSON"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json_data = {
                    "alarm_name": data[0].get("alarm_name", "unknown") if data else "unknown",
                    "start_time": self._alarm_start_time.isoformat() if self._alarm_start_time else None,
                    "data": [
                        {
                            "timestamp": dp["timestamp"].isoformat(),
                            "discrete": {f"input_{k}": v for k, v in dp["discrete"].items()},
                            "analog": {
                                "current": {f"input_{k}": v for k, v in dp["analog"].items() 
                                           if k < self.config.analog_current_inputs},
                                "voltage": {f"input_{k}": v for k, v in dp["analog"].items() 
                                           if k >= self.config.analog_current_inputs}
                            }
                        }
                        for dp in data
                    ]
                }
                json.dump(json_data, f, ensure_ascii=False, indent=2)
                if self.config.fsync_on_write:
                    os.fsync(f.fileno())
        except OSError as exc:
            logger.error("Ошибка при записи аварийного JSON файла %s: %s", file_path, exc)
            if self.config.enable_backup_storage:
                for dp in data:
                    self._backup_queue.append(dp)
                self._write_alarm_backup()

    # ---------- Резервное хранение аварий ----------

    def _alarm_backup_file_path(self) -> Path:
        return Path(self.config.backup_directory) / "backup_alarms.jsonl"

    def _write_alarm_backup(self) -> None:
        """Записать буфер аварийных данных в резервный файл."""
        if not self.config.enable_backup_storage or not self._backup_queue:
            return
        path = self._alarm_backup_file_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                while self._backup_queue:
                    dp = self._backup_queue.popleft()
                    serializable = {
                        "timestamp": dp["timestamp"].isoformat(),
                        "discrete": dp["discrete"],
                        "analog": dp["analog"],
                    }
                    json.dump(serializable, f, ensure_ascii=False)
                    f.write("\n")
            logger.warning("Записан резервный аварийный буфер в %s", path)
        except OSError as exc:
            logger.error("Не удалось записать аварийные резервные данные в %s: %s", path, exc)
    
    def close(self):
        """Закрыть writer"""
        # Не блокируемся на lock при конкурентной записи.
        if not self._lock.acquire(timeout=0.2):
            return
        try:
            self._buffer.clear()
        finally:
            self._lock.release()
