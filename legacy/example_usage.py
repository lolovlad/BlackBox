"""
Пример использования библиотеки BlackBox

Этот скрипт демонстрирует, как использовать библиотеку для регистрации данных.
В реальном проекте вы должны заменить функции read_* на реальное чтение с датчиков.
"""

import time
from blackbox import DataLogger, DataLoggerConfig, AlarmCondition, DataFormat
from modbus_reader import read_all_data


# Симуляция чтения данных с датчиков (в реальном проекте замените на реальные функции)
def read_discrete_sensor(index: int) -> bool:
    """Симуляция чтения дискретного датчика"""
    # В реальном проекте здесь будет код чтения с GPIO или другого интерфейса
    return False


def read_current_sensor(index: int) -> float:
    """Симуляция чтения токового датчика"""
    # В реальном проекте здесь будет код чтения с ADC
    return 5.0


def read_voltage_sensor(index: int) -> float:
    """Симуляция чтения датчика напряжения"""
    # В реальном проекте здесь будет код чтения с ADC
    return 220.0


def main():
    # Создание конфигурации
    config = DataLoggerConfig(
        data_directory="./data",
        alarm_directory="./alarms",
        analog_poll_interval=0.1,
        max_discrete_inputs=20,
        analog_current_inputs=3,
        analog_voltage_inputs=3,
        data_format=DataFormat.CSV,
        overwrite_data=True,
        overwrite_alarms=False,
        alarm_pre_time=300,  # 5 минут
        alarm_post_time=900,  # 15 минут
        # Настройка имен колонок
        csv_column_names={
            "timestamp": "Время",
            "discrete_0": "Датчик_1",
            "discrete_1": "Датчик_2",
            "current_0": "Ток_Генератор_1_А",
            "current_1": "Ток_Генератор_2_А",
            "current_2": "Ток_Генератор_3_А",
            "voltage_0": "Напряжение_Генератор_1_В",
            "voltage_1": "Напряжение_Генератор_2_В",
            "voltage_2": "Напряжение_Генератор_3_В"
        }
    )
    
    # Создание регистратора
    logger = DataLogger(config)
    
    # Настройка аварийных условий
    
    # Условие 1: Перегрузка по току генератора 1
    alarm1 = AlarmCondition(
        name="Перегрузка_Генератор_1",
        analog_inputs=[0],  # Токовый вход 0
        threshold_max=50.0  # Максимум 50 А
    )
    logger.add_alarm_condition(alarm1)
    
    # Условие 2: Авария дискретного датчика
    alarm2 = AlarmCondition(
        name="Авария_Датчик_1",
        discrete_inputs=[0],
        discrete_condition=lambda d: d.get(0, False) == True
    )
    logger.add_alarm_condition(alarm2)
    
    # Условие 3: Комплексное условие
    def check_complex(discrete_values, analog_values):
        # Авария если датчик 1 активен И ток превышает 40 А
        return (discrete_values.get(1, False) == True and 
                analog_values.get(0, 0.0) > 40.0)
    
    alarm3 = AlarmCondition(
        name="Комплексная_Авария",
        discrete_inputs=[1],
        analog_inputs=[0],
        discrete_condition=lambda d: d.get(1, False) == True,
        analog_condition=lambda a: a.get(0, 0.0) > 40.0
    )
    logger.add_alarm_condition(alarm3)
    
    # Запуск регистратора
    print("Запуск регистратора данных...")
    logger.start()
    
    try:
        # Основной цикл работы
        print("Регистратор запущен. Нажмите Ctrl+C для остановки.")
        iteration = 0
        
        while True:
            # Пример чтения с контроллера ДГУ по Modbus RTU
            try:
                modbus_data = read_all_data()
                logger.update_from_modbus_data(modbus_data)
                print(modbus_data)
            except Exception as exc:
                print(f"Ошибка Modbus чтения: {exc}")

            # Чтение данных с датчиков
            # Дискретные входы
            for i in range(20):
                value = read_discrete_sensor(i)
                logger.set_discrete_value(i, value)
            
            # Токовые входы
            for i in range(3):
                value = read_current_sensor(i)
                logger.set_current_value(i, value)
            
            # Входы напряжения
            for i in range(3):
                value = read_voltage_sensor(i)
                logger.set_voltage_value(i, value)
            
            # Для демонстрации: периодически меняем значения
            iteration += 1
            if iteration % 100 == 0:
                print(f"Итерация {iteration}, статус: работа")
                # Можно установить тестовые значения для проверки аварий
                # logger.set_discrete_value(0, True)
                # logger.set_current_value(0, 55.0)
            
            time.sleep(0.1)  # Интервал соответствует analog_poll_interval
            
    except KeyboardInterrupt:
        print("\nОстановка регистратора...")
    finally:
        logger.stop()
        print("Регистратор остановлен.")


if __name__ == "__main__":
    main()
