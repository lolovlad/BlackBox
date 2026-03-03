# BlackBox - Библиотека для регистрации данных

## Краткое описание

Библиотека для сбора и хранения данных с дискретных и аналоговых входов на Raspberry Pi 5. Предназначена для использования в системах мониторинга и регистрации данных.

## Основные компоненты

### 1. `config.py` - Конфигурация
- `DataLoggerConfig` - Основная конфигурация регистратора
- `AlarmCondition` - Условия аварийных событий
- `DataFormat` - Форматы данных (CSV, JSON)

### 2. `discrete_inputs.py` - Дискретные входы
- `DiscreteInputs` - Управление до 20 дискретных входов
- Опрос по изменению статуса
- Callbacks при изменении значений

### 3. `analog_inputs.py` - Аналоговые входы
- `AnalogInputs` - Управление аналоговыми входами
- 3 токовых входа
- 3 входа напряжения генератора

### 4. `data_writer.py` - Запись данных
- `DataWriter` - Запись обычных данных (разделение по дням)
- `AlarmWriter` - Запись аварийных событий (5 мин до, 15 мин после)

### 5. `data_logger.py` - Главный класс
- `DataLogger` - Основной класс для управления всеми компонентами
- Автоматический опрос входов
- Мониторинг аварийных событий
- Потокобезопасная работа

## Использование

```python
from blackbox import DataLogger, DataLoggerConfig, AlarmCondition

# Конфигурация
config = DataLoggerConfig(
    data_directory="./data",
    alarm_directory="./alarms"
)

# Создание регистратора
logger = DataLogger(config)

# Настройка аварийных условий
alarm = AlarmCondition(
    name="Перегрузка",
    analog_inputs=[0],
    threshold_max=50.0
)
logger.add_alarm_condition(alarm)

# Запуск
logger.start()

# В вашем скрипте чтения датчиков:
logger.set_discrete_value(0, True)
logger.set_current_value(0, 5.5)
logger.set_voltage_value(0, 220.0)

# Остановка
logger.stop()
```

## Важно

- Библиотека НЕ содержит код для чтения данных с физических датчиков
- Библиотека НЕ содержит функционал видеонаблюдения
- Данные должны передаваться через методы `set_*_value()`
