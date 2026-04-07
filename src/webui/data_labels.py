"""Подписи колонок = оригинальные имена полей из modbus_acquire.deif (без перевода)."""

from __future__ import annotations

from modbus_acquire.deif import ANALOG_CSV_COLUMNS, DISCRETE_CSV_COLUMNS


def analog_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    """(ключ, отображаемое имя) — оба совпадают с именем в таблице/DEIF."""
    return [(k, k) for k in keys]


def discrete_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    return [(k, k) for k in keys]


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
