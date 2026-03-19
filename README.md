# BlackBox

Библиотека для сбора, нормализации и записи телеметрии (дискретные/аналоговые сигналы) с поддержкой аварийных событий и интеграции с Modbus RTU.

## Возможности

- До 20 дискретных входов с обработкой изменений
- Аналоговые входы (ток/напряжение) с периодическим опросом
- Запись в `CSV` или `JSON` с ротацией по дням
- Аварийные события: буфер до/после события и запись в отдельные файлы
- Резервное хранение при ошибках записи
- Гибкое чтение Modbus RTU (`minimalmodbus`): scaling, 16/32-bit, bitfield, byte order

## Установка

```bash
pip install -r requirements.txt
```

## Быстрый старт

### 1) Базовый DataLogger

```python
from blackbox import DataLogger, DataLoggerConfig

config = DataLoggerConfig(
    data_directory="./data",
    alarm_directory="./alarms",
    analog_poll_interval=0.1,
)

logger = DataLogger(config)
logger.start()

logger.set_discrete_value(0, True)
logger.set_current_value(0, 5.5)
logger.set_voltage_value(0, 220.0)

logger.stop()
```

### 2) Контекстный менеджер

```python
from blackbox import DataLogger, DataLoggerConfig

with DataLogger(DataLoggerConfig()) as logger:
    logger.set_discrete_value(0, True)
    logger.set_current_value(0, 5.5)
```

## Работа с Modbus RTU

По умолчанию используется:

- порт: `/dev/ttyAMA0`
- режим: RTU
- параметры: `9600 8N1`
- slave id: `1`

### 1) Простое чтение

```python
from modbus_reader import read_all_data

data = read_all_data()
print(data)
```

Ожидаемый формат:

```python
{
    "voltage_L1": 230.4,
    "frequency": 50.0,
    "power": 125.6,
    "engine_rpm": 1500,
    "alarms": ["overspeed"]
}
```

### 2) Кастомная карта регистров (`fields`)

```python
from modbus_reader import read_all_data

data = read_all_data({
    "fields": [
        {"name": "freq", "address": 3, "reg_type": "input", "data_type": "u16", "scale": 0.01},
        {"name": "power", "address": 10, "reg_type": "input", "data_type": "s32", "scale": 0.1, "byteorder": "big"},
        {"name": "alarms", "address": 20, "reg_type": "input", "data_type": "bitfield",
         "bit_labels": {0: "low_oil", 1: "high_temp"}},
    ],
    "include_raw": True,
})

print(data)
```

Поддерживаемые поля:

- `reg_type`: `input` или `holding`
- `data_type`: `u16`, `s16`, `u32`, `s32`, `bitfield`
- `byteorder`: `big`, `little`, `big_swap`, `little_swap` (для 32-bit)
- `scale`, `decimals`

### 3) Пример Holding Registers

```python
from modbus_reader import read_all_data

data = read_all_data({
    "fields": [
        {"name": "oil_pressure", "address": 100, "reg_type": "holding", "data_type": "u16", "scale": 0.1},
        {"name": "coolant_temp", "address": 101, "reg_type": "holding", "data_type": "s16", "scale": 0.1},
    ]
})
```

### 4) 32-bit с другим порядком слов/байтов

```python
from modbus_reader import read_all_data

data = read_all_data({
    "fields": [
        {
            "name": "energy_total_kwh",
            "address": 300,
            "reg_type": "input",
            "data_type": "u32",
            "byteorder": "little_swap",
            "scale": 0.01,
            "decimals": 2,
        }
    ]
})

print(data["energy_total_kwh"])
```

## Интеграция Modbus в DataLogger

### Автоматический опрос в отдельном потоке

```python
from blackbox import DataLogger, DataLoggerConfig

config = DataLoggerConfig(
    modbus_enabled=True,
    modbus_poll_interval=0.5,
    modbus_reader_config={
        "slave_id": 1,
        # "fields": [...]  # при необходимости переопределите карту регистров
    },
)

logger = DataLogger(config)
logger.start()
```

### Ручное обновление (без фонового Modbus-потока)

```python
from blackbox import DataLogger, DataLoggerConfig
from modbus_reader import read_all_data

logger = DataLogger(DataLoggerConfig(modbus_enabled=False))
logger.start()

data = read_all_data()
logger.update_from_modbus_data(data)
```

### Подмена источника Modbus (максимальная гибкость)

```python
from blackbox import DataLogger, DataLoggerConfig
from modbus_reader import read_all_data

logger = DataLogger(DataLoggerConfig(modbus_enabled=True))

def my_modbus_reader():
    return read_all_data({
        "slave_id": 2,
        "timeout": 1.5,
        "retry_count": 5,
        "fields": [
            {"name": "frequency", "address": 3, "reg_type": "input", "data_type": "u16", "scale": 0.01},
            {"name": "power", "address": 10, "reg_type": "input", "data_type": "s32", "scale": 0.1},
            {"name": "alarms", "address": 20, "reg_type": "input", "data_type": "bitfield",
             "bit_labels": {0: "low_oil_pressure", 1: "high_coolant_temp"}},
        ],
    })

logger.set_modbus_reader(my_modbus_reader)
logger.start()
```

## Конфигурация `DataLoggerConfig`

Базовые параметры:

- `data_directory`, `alarm_directory`, `backup_directory`, `log_directory`
- `max_discrete_inputs` (1..20)
- `analog_current_inputs`, `analog_voltage_inputs`
- `analog_poll_interval`
- `data_format` (`CSV` / `JSON`)
- `overwrite_data`, `overwrite_alarms`
- `alarm_pre_time`, `alarm_post_time`
- `enable_backup_storage`
- `log_level`, `log_to_console`

Параметры Modbus-интеграции:

- `modbus_enabled`
- `modbus_poll_interval`
- `modbus_reader_config`
- `modbus_to_analog_map`
- `modbus_alarm_bits_to_discrete_map`

Пример:

```python
from blackbox import DataLoggerConfig, DataFormat

config = DataLoggerConfig(
    data_directory="./data",
    alarm_directory="./alarms",
    data_format=DataFormat.CSV,
    analog_poll_interval=0.1,
    modbus_enabled=True,
    modbus_poll_interval=0.5,
)
```

## Аварийные события

### Простое условие по дискретному входу

```python
from blackbox import DataLogger, DataLoggerConfig, AlarmCondition

logger = DataLogger(DataLoggerConfig())

alarm = AlarmCondition(
    name="Авария_Датчик_1",
    discrete_inputs=[0],
    discrete_condition=lambda d: d.get(0, False) is True,
)

logger.add_alarm_condition(alarm)
logger.start()
```

### Порог по аналоговому входу

```python
from blackbox import AlarmCondition

alarm = AlarmCondition(
    name="Перегрузка_Ток",
    analog_inputs=[0],
    threshold_max=10.0,
)
```

## Формат хранения данных

Обычные данные:

```text
data/
  2026-03-19/
    data.csv
```

Аварийные события:

```text
alarms/
  alarm_ИмяСобытия_YYYYMMDD_HHMMSS.csv
```

Каждый аварийный файл включает данные:

- за `alarm_pre_time` секунд до события
- во время события
- за `alarm_post_time` секунд после события

## Ограничения и примечания

- Библиотека не читает физические датчики напрямую: это делает ваш код, а затем передает значения в `DataLogger`.
- Для Modbus требуется `minimalmodbus` (уже в `requirements.txt`).
- Потокобезопасность обеспечивается внутри модулей (`threading.Lock`).

## API (кратко)

- `DataLogger`: `start()`, `stop()`, `update_from_modbus_data()`, `set_modbus_reader()`, методы `set/get` входов
- `DataLoggerConfig`: конфигурация логгера/хранилища/Modbus
- `AlarmCondition`: условия аварийных событий
- `read_all_data()`: быстрое чтение Modbus данных

## Лицензия

MIT
