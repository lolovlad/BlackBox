"""Русские подписи колонок для таблиц и CSV (ключи в БД остаются английскими)."""

from __future__ import annotations

from modbus_acquire.deif import ANALOG_CSV_COLUMNS, DISCRETE_CSV_COLUMNS

RU_ANALOG_LABELS: dict[str, str] = {
    "UgenL1L2": "U ген L1–L2, В",
    "UgenL2L3": "U ген L2–L3, В",
    "UgenL3L1": "U ген L3–L1, В",
    "UgenL1N": "U ген L1–N, В",
    "UgenL2N": "U ген L2–N, В",
    "UgenL3N": "U ген L3–N, В",
    "UbusL1L2": "U шина L1–L2, В",
    "UbusL2L3": "U шина L2–L3, В",
    "UbusL3L1": "U шина L3–L1, В",
    "UbusL1N": "U шина L1–N, В",
    "UbusL2N": "U шина L2–N, В",
    "UbusL3N": "U шина L3–N, В",
    "Usupply": "U питания, В",
    "IL1": "I L1, А",
    "IL2": "I L2, А",
    "IL3": "I L3, А",
    "Fgen": "F ген, Гц",
    "Fbus": "F шины, Гц",
    "Pgen": "P ген, кВт",
    "Qgen": "Q ген, кВар",
    "Sgen": "S ген, кВА",
    "PF": "cos φ",
    "RPM": "Обороты, об/мин",
    "PT100_1": "PT100_1, °C",
    "PT100_2": "PT100_2, °C",
    "Egen": "E ген, кВт·ч",
    "Runhours_hours": "Наработка, ч",
    "Analog_input_E4": "Аналог. вход E4",
    "Alarms_total": "Аварий всего",
    "Alarms_non_ack": "Аварий не квит.",
    "Gov.Reg.Value": "Рег. GOV, %",
    "AVR Reg.Value": "Рег. AVR, %",
}

RU_DISCRETE_LABELS: dict[str, str] = {
    "Engine_running": "Двигатель работает",
    "Engine_cooling_down": "Охлаждение двигателя",
    "Engine_stopped": "Двигатель остановлен",
    "CB_Closed": "Выкл. закрыт",
    "CB_Opened": "Выкл. открыт",
    "CB_Tripped": "Выкл. отключён",
    "Warning": "Предупреждение",
    "Shutdown": "Аварийный останов",
    "Avail_to_sync": "Готов к синхронизации",
    "Synchronizing": "Синхронизация",
    "Auto_control_on": "Автоуправление",
    "Local_control_on": "Местное управление",
    "ManMode": "Ручной режим",
    "Peak Lopping": "Peak Lopping",
    "Base Load (P/PF)": "Базовая нагрузка (P/PF)",
    "Droop": "Дроп",
    "Load Share": "Разделение нагрузки",
    "Base Load (P/var)": "Базовая нагрузка (P/var)",
}


def analog_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    return [(k, RU_ANALOG_LABELS.get(k, k)) for k in keys]


def discrete_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    return [(k, RU_DISCRETE_LABELS.get(k, k)) for k in keys]


def all_analog_keys() -> list[str]:
    return list(ANALOG_CSV_COLUMNS)


def all_discrete_keys() -> list[str]:
    return list(DISCRETE_CSV_COLUMNS)


def filter_valid_analog(requested: list[str] | None) -> list[str]:
    allowed = set(ANALOG_CSV_COLUMNS)
    if not requested:
        return list(ANALOG_CSV_COLUMNS)
    return [k for k in requested if k in allowed]


def filter_valid_discrete(requested: list[str] | None) -> list[str]:
    allowed = set(DISCRETE_CSV_COLUMNS)
    if not requested:
        return list(DISCRETE_CSV_COLUMNS)
    return [k for k in requested if k in allowed]
