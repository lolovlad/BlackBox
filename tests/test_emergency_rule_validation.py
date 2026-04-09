import pytest

from src.webui.emergency_rule_validation import (
    build_rule_validation_sets,
    evaluate_emergency_rule_expression,
    validate_emergency_rule_expression,
)

pytest.importorskip("simpleeval")

_MIN_CFG = {
    "fields": [
        {"name": "UbusL1L2", "type": "uint16"},
        {"name": "Engine_running", "type": "bool"},
        {"name": "AlarmReg_20", "type": "bitfield", "bits": {"0": "1010 BUS High Volt 1"}},
        {"name": "Gov.Reg.Value", "type": "uint16", "expr": "x / 10"},
    ]
}


def test_build_rule_validation_sets_collects_bits():
    names, errors = build_rule_validation_sets(_MIN_CFG)
    assert "UbusL1L2" in names
    assert "AlarmReg_20" in names
    assert "Gov.Reg.Value" in names
    assert "1010 BUS High Volt 1" in errors


def test_validate_accepts_analog_compare():
    ok, err = validate_emergency_rule_expression("UbusL1L2 > 10", settings_config=_MIN_CFG)
    assert ok and err is None


def test_validate_rejects_unknown_field():
    ok, err = validate_emergency_rule_expression("UnknownField > 1", settings_config=_MIN_CFG)
    assert not ok
    assert err and "Неизвестная переменная" in err


def test_validate_dotted_field_name():
    ok, err = validate_emergency_rule_expression("Gov.Reg.Value > 0", settings_config=_MIN_CFG)
    assert ok and err is None


def test_validate_alarm_string_in_bitfield():
    ok, err = validate_emergency_rule_expression(
        "'1010 BUS High Volt 1' in AlarmReg_20",
        settings_config=_MIN_CFG,
    )
    assert ok and err is None


def test_validate_rejects_unknown_alarm_label():
    ok, err = validate_emergency_rule_expression(
        "'no such alarm' in AlarmReg_20",
        settings_config=_MIN_CFG,
    )
    assert not ok
    assert err and "Неизвестная строка аварии" in err


def test_validate_requires_bool_result():
    ok, err = validate_emergency_rule_expression("UbusL1L2 + 1", settings_config=_MIN_CFG)
    assert not ok
    assert err and "логическое значение" in err


def test_evaluate_missing_runtime_field_is_not_error() -> None:
    ok, fired, err = evaluate_emergency_rule_expression("Usupply > 20", processed={"active_alarms": []})
    assert ok is True
    assert fired is False
    assert err and "Пропущено поле" in err
