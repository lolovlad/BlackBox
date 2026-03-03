# BlackBox - Библиотека для регистрации данных на Raspberry Pi 5

Библиотека для сбора и хранения данных с дискретных и аналоговых входов на Raspberry Pi 5. Предоставляет гибкую систему настройки условий записи, включая аварийные события с предварительной и последующей записью.

## Основные возможности

- **Контроль дискретных входов**: До 20 дискретных входов с опросом по изменению статуса
- **Контроль аналоговых входов**: 
  - 3 токовых входа
  - 3 входа напряжения генератора
  - Опрос с дискретностью 0.1 сек
- **Гибкая запись данных**: 
  - Разделение по дням (папка с датой и CSV файлом)
  - Возможность перезаписи обычных данных
  - Настраиваемый формат данных (CSV, JSON)
- **Аварийные события**:
  - Гибкая настройка условий срабатывания
  - Запись 5 минут до события и 15 минут после
  - Сохранение в отдельную папку без возможности перезаписи

## Установка

```bash
pip install -r requirements.txt
```

## Быстрый старт

### Базовое использование

```python
from blackbox import DataLogger, DataLoggerConfig

# Создание конфигурации
config = DataLoggerConfig(
    data_directory="./data",
    alarm_directory="./alarms",
    analog_poll_interval=0.1
)

# Создание и запуск регистратора
logger = DataLogger(config)
logger.start()

# В вашем скрипте чтения данных с датчиков:
# Установка значений дискретных входов
logger.set_discrete_value(0, True)
logger.set_discrete_value(1, False)

# Установка значений аналоговых входов
logger.set_current_value(0, 5.5)  # Ток, вход 0
logger.set_voltage_value(0, 220.0)  # Напряжение, вход 0

# Остановка регистратора
logger.stop()
```

### Использование с контекстным менеджером

```python
from blackbox import DataLogger, DataLoggerConfig

config = DataLoggerConfig()
with DataLogger(config) as logger:
    # Ваш код работы с датчиками
    logger.set_discrete_value(0, True)
    logger.set_current_value(0, 5.5)
    # Регистратор автоматически остановится при выходе из блока
```

## Конфигурация

### Базовая конфигурация

```python
from blackbox import DataLoggerConfig, DataFormat

config = DataLoggerConfig(
    data_directory="./data",           # Директория для обычных данных
    alarm_directory="./alarms",        # Директория для аварийных событий
    max_discrete_inputs=20,            # Максимум дискретных входов
    analog_current_inputs=3,           # Количество токовых входов
    analog_voltage_inputs=3,           # Количество входов напряжения
    analog_poll_interval=0.1,          # Интервал опроса аналоговых входов (сек)
    discrete_poll_on_change=True,      # Опрос дискретных по изменению
    data_format=DataFormat.CSV,        # Формат данных (CSV или JSON)
    overwrite_data=True,               # Перезапись обычных данных
    overwrite_alarms=False,            # Перезапись аварийных данных
    alarm_pre_time=300,                # Время записи до события (сек) - 5 минут
    alarm_post_time=900                # Время записи после события (сек) - 15 минут
)
```

### Настройка формата CSV

Вы можете настроить порядок и имена колонок в CSV файле:

```python
config = DataLoggerConfig(
    csv_column_order=[
        "timestamp",
        "discrete_0", "discrete_1", "discrete_2",
        "current_0", "current_1", "current_2",
        "voltage_0", "voltage_1", "voltage_2"
    ],
    csv_column_names={
        "timestamp": "Время",
        "discrete_0": "Датчик_1",
        "current_0": "Ток_Генератор_1",
        "voltage_0": "Напряжение_Генератор_1"
    },
    csv_delimiter=","
)
```

## Аварийные события

### Простое условие на дискретные входы

```python
from blackbox import DataLogger, DataLoggerConfig, AlarmCondition

config = DataLoggerConfig()
logger = DataLogger(config)

# Условие: если вход 0 стал True
def check_discrete(discrete_values):
    return discrete_values.get(0, False) == True

alarm = AlarmCondition(
    name="Авария_Датчик_1",
    discrete_inputs=[0],
    discrete_condition=check_discrete
)

logger.add_alarm_condition(alarm)
logger.start()
```

### Условие на аналоговые входы (пороговые значения)

```python
# Условие: если ток на входе 0 превышает 10 А
alarm = AlarmCondition(
    name="Перегрузка_Ток",
    analog_inputs=[0],  # Токовый вход 0
    threshold_max=10.0  # Максимальное значение
)

logger.add_alarm_condition(alarm)
```

### Комплексное условие (дискреты + аналоги)

```python
def check_complex(discrete_values, analog_values):
    # Проверка дискретного входа
    if not discrete_values.get(0, False):
        return False
    # Проверка аналогового входа
    if analog_values.get(0, 0.0) > 10.0:
        return True
    return False

alarm = AlarmCondition(
    name="Комплексная_Авария",
    discrete_inputs=[0],
    analog_inputs=[0],
    discrete_condition=lambda d: d.get(0, False) == True,
    analog_condition=lambda a: a.get(0, 0.0) > 10.0
)

logger.add_alarm_condition(alarm)
```

### Условие с несколькими входами

```python
# Авария если любой из входов 0, 1, 2 стал True
def check_multiple(discrete_values):
    return any(discrete_values.get(i, False) for i in [0, 1, 2])

alarm = AlarmCondition(
    name="Авария_Группа_Датчиков",
    discrete_inputs=[0, 1, 2],
    discrete_condition=check_multiple
)

logger.add_alarm_condition(alarm)
```

## API Reference

### DataLogger

Главный класс регистратора данных.

#### Методы управления

- `start()` - Запустить регистратор
- `stop()` - Остановить регистратор
- `is_running() -> bool` - Проверить статус работы

#### Методы работы с дискретными входами

- `set_discrete_value(input_index: int, value: bool)` - Установить значение дискретного входа
- `get_discrete_value(input_index: int) -> bool` - Получить значение дискретного входа
- `get_all_discrete_values() -> Dict[int, bool]` - Получить все значения дискретных входов

#### Методы работы с аналоговыми входами

- `set_current_value(input_index: int, value: float)` - Установить значение токового входа
- `set_voltage_value(input_index: int, value: float)` - Установить значение входа напряжения
- `get_current_value(input_index: int) -> float` - Получить значение токового входа
- `get_voltage_value(input_index: int) -> float` - Получить значение входа напряжения
- `get_all_analog_values() -> Dict[int, float]` - Получить все значения аналоговых входов

#### Методы работы с аварийными условиями

- `add_alarm_condition(condition: AlarmCondition)` - Добавить условие аварийного события
- `remove_alarm_condition(name: str)` - Удалить условие по имени
- `get_alarm_conditions() -> List[AlarmCondition]` - Получить список всех условий

### DataLoggerConfig

Класс конфигурации регистратора.

#### Основные параметры

- `data_directory: str` - Директория для обычных данных
- `alarm_directory: str` - Директория для аварийных событий
- `max_discrete_inputs: int` - Максимум дискретных входов (1-20)
- `analog_current_inputs: int` - Количество токовых входов
- `analog_voltage_inputs: int` - Количество входов напряжения
- `analog_poll_interval: float` - Интервал опроса аналоговых входов (сек)
- `discrete_poll_on_change: bool` - Опрос дискретных по изменению
- `data_format: DataFormat` - Формат данных (CSV, JSON)
- `overwrite_data: bool` - Перезапись обычных данных
- `overwrite_alarms: bool` - Перезапись аварийных данных
- `alarm_pre_time: int` - Время записи до события (сек)
- `alarm_post_time: int` - Время записи после события (сек)

#### Параметры формата CSV

- `csv_delimiter: str` - Разделитель в CSV
- `csv_include_timestamp: bool` - Включать временную метку
- `csv_include_discrete: bool` - Включать дискретные входы
- `csv_include_analog: bool` - Включать аналоговые входы
- `csv_column_order: List[str]` - Порядок колонок
- `csv_column_names: Dict[str, str]` - Имена колонок

### AlarmCondition

Класс для определения условий аварийных событий.

#### Параметры

- `name: str` - Имя условия
- `discrete_inputs: Optional[List[int]]` - Номера дискретных входов для мониторинга
- `analog_inputs: Optional[List[int]]` - Номера аналоговых входов для мониторинга
- `discrete_condition: Optional[Callable]` - Функция проверки дискретов
- `analog_condition: Optional[Callable]` - Функция проверки аналогов
- `threshold_min: Optional[float]` - Минимальное значение для аналогов
- `threshold_max: Optional[float]` - Максимальное значение для аналогов

#### Методы

- `check(discrete_values: Dict[int, bool], analog_values: Dict[int, float]) -> bool` - Проверка условия

## Структура файлов данных

### Обычные данные

Данные сохраняются в структуре:
```
data/
  2024-01-15/
    data.csv
  2024-01-16/
    data.csv
```

### Аварийные события

Аварийные события сохраняются в структуре:
```
alarms/
  alarm_Авария_Датчик_1_20240115_143022.csv
  alarm_Перегрузка_Ток_20240115_150315.csv
```

Каждый файл аварийного события содержит:
- 5 минут данных до события
- Данные во время события
- 15 минут данных после события

## Примеры использования

### Пример 1: Простой регистратор

```python
from blackbox import DataLogger, DataLoggerConfig

config = DataLoggerConfig(
    data_directory="/mnt/ssd/data",
    analog_poll_interval=0.1
)

logger = DataLogger(config)
logger.start()

# В цикле чтения датчиков:
while True:
    # Чтение данных с датчиков (ваш код)
    discrete_0 = read_discrete_sensor(0)
    current_0 = read_current_sensor(0)
    
    # Передача данных в регистратор
    logger.set_discrete_value(0, discrete_0)
    logger.set_current_value(0, current_0)
    
    time.sleep(0.1)
```

### Пример 2: С аварийными событиями

```python
from blackbox import DataLogger, DataLoggerConfig, AlarmCondition

config = DataLoggerConfig(
    data_directory="/mnt/ssd/data",
    alarm_directory="/mnt/ssd/alarms"
)

logger = DataLogger(config)

# Настройка аварийных условий
alarm1 = AlarmCondition(
    name="Перегрузка_Генератор_1",
    analog_inputs=[0],  # Токовый вход генератора 1
    threshold_max=50.0  # Максимум 50 А
)

alarm2 = AlarmCondition(
    name="Авария_Датчик_Двери",
    discrete_inputs=[5],
    discrete_condition=lambda d: d.get(5, False) == True
)

logger.add_alarm_condition(alarm1)
logger.add_alarm_condition(alarm2)

logger.start()

# Ваш код работы с датчиками...
```

### Пример 3: Кастомный формат CSV

```python
config = DataLoggerConfig(
    csv_column_order=[
        "timestamp",
        "discrete_0", "discrete_1",
        "current_0", "current_1", "current_2",
        "voltage_0", "voltage_1", "voltage_2"
    ],
    csv_column_names={
        "timestamp": "Время_измерения",
        "discrete_0": "Датчик_Двери",
        "discrete_1": "Датчик_Окна",
        "current_0": "Ток_Генератор_1_А",
        "current_1": "Ток_Генератор_2_А",
        "current_2": "Ток_Генератор_3_А",
        "voltage_0": "Напряжение_Генератор_1_В",
        "voltage_1": "Напряжение_Генератор_2_В",
        "voltage_2": "Напряжение_Генератор_3_В"
    },
    csv_delimiter=";"
)
```

## Важные замечания

1. **Чтение данных с датчиков**: Библиотека НЕ содержит код для чтения данных с физических датчиков. Вы должны реализовать это в своем скрипте и передавать данные через методы `set_discrete_value()`, `set_current_value()`, `set_voltage_value()`.

2. **Видеонаблюдение**: Библиотека НЕ содержит функционал видеонаблюдения. Это должно быть реализовано отдельно.

3. **Потокобезопасность**: Все операции с данными потокобезопасны и могут использоваться из разных потоков.

4. **Производительность**: Запись данных выполняется асинхронно в отдельных потоках, не блокируя основной поток чтения датчиков.

5. **SSD диск**: Убедитесь, что директории `data_directory` и `alarm_directory` указывают на SSD диск для оптимальной производительности.

## Требования

- Python 3.7+
- Стандартная библиотека Python (os, csv, json, threading, datetime, pathlib)

## Лицензия

MIT License
