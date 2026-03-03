from blackbox.discrete_inputs import DiscreteInputs
from blackbox.analog_inputs import AnalogInputs


def test_discrete_inputs_basic_and_change_detection():
    di = DiscreteInputs(max_inputs=4)
    changes = []

    def cb(idx, old, new):
        changes.append((idx, old, new))

    di.register_change_callback(0, cb)
    assert di.get_value(0) is False

    # Первое изменение
    changed = di.set_value(0, True)
    assert changed is True
    assert di.get_value(0) is True
    assert di.has_changed(0) is True
    assert changes[-1] == (0, False, True)

    # Повтор того же значения — не изменение
    changed = di.set_value(0, True)
    assert changed is False

    all_values = di.get_all_values()
    assert all_values[0] is True


def test_analog_inputs_basic():
    ai = AnalogInputs(current_inputs=3, voltage_inputs=3)

    ai.set_current_value(0, 5.5)
    ai.set_voltage_value(1, 220.0)

    assert ai.get_current_value(0) == 5.5
    assert ai.get_voltage_value(1) == 220.0

    all_current = ai.get_all_current_values()
    all_voltage = ai.get_all_voltage_values()
    assert all_current[0] == 5.5
    assert all_voltage[1] == 220.0

    all_values = ai.get_all_values()
    # Токи по индексам 0..2
    assert all_values[0] == 5.5
    # Напряжения смещены на количество токовых входов
    assert all_values[3 + 1] == 220.0

