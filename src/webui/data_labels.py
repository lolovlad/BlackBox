"""Подписи/ключи колонок из settings/settings.json."""

from __future__ import annotations

from src.webui.modbus_service import _load_settings, analog_discrete_keys


def analog_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    labels = _labels_map()
    return [(k, labels.get(k, k)) for k in keys]


def discrete_labels_for(keys: list[str]) -> list[tuple[str, str]]:
    labels = _labels_map()
    return [(k, labels.get(k, k)) for k in keys]


def all_analog_keys() -> list[str]:
    analog, _ = analog_discrete_keys()
    return analog


def all_discrete_keys() -> list[str]:
    _, discrete = analog_discrete_keys()
    return discrete


def filter_valid_analog(requested: list[str] | None) -> list[str]:
    analog, _ = analog_discrete_keys()
    allowed = set(analog)
    if not requested:
        return list(analog)
    return [k for k in requested if k in allowed]


def filter_valid_discrete(requested: list[str] | None) -> list[str]:
    _, discrete = analog_discrete_keys()
    allowed = set(discrete)
    if not requested:
        return list(discrete)
    return [k for k in requested if k in allowed]


def _labels_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for field in _load_settings().get("fields", []):
        name = field.get("name")
        if not name:
            continue
        label = field.get("display_name") or field.get("title") or field.get("ru_name") or field.get("label") or name
        mapping[str(name)] = str(label)
    mapping.setdefault("modbus_reading", "Чтение по Modbus")
    for idx in range(1, 9):
        mapping.setdefault(f"gpio_{idx}", f"GPIO {idx}")
    return mapping
