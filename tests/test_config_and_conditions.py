import logging
from datetime import datetime

from blackbox.config import DataLoggerConfig, AlarmCondition, DataFormat


def test_config_validation_defaults():
    cfg = DataLoggerConfig()
    assert cfg.validate() is True
    assert cfg.max_discrete_inputs == 20
    assert cfg.analog_poll_interval == 0.1
    assert cfg.data_format == DataFormat.CSV


def test_config_invalid_discrete_inputs():
    cfg = DataLoggerConfig(max_discrete_inputs=0)
    try:
        cfg.validate()
    except ValueError as exc:
        assert "max_discrete_inputs" in str(exc)
    else:
        assert False, "Ожидалось исключение при некорректном количестве дискретных входов"


def test_alarm_condition_discrete_only():
    cond = AlarmCondition(
        name="DISCRETE_ON",
        discrete_inputs=[0, 1],
        discrete_condition=lambda d: d.get(0, False) and not d.get(1, False),
    )
    assert cond.check({0: True, 1: False}, {}) is True
    assert cond.check({0: False, 1: False}, {}) is False


def test_alarm_condition_analog_thresholds():
    cond = AlarmCondition(
        name="ANALOG_RANGE",
        analog_inputs=[0],
        threshold_min=1.0,
        threshold_max=10.0,
    )
    assert cond.check({}, {0: 5.0}) is True
    # Ниже минимума
    assert cond.check({}, {0: 0.5}) is False
    # Выше максимума
    assert cond.check({}, {0: 11.0}) is False

