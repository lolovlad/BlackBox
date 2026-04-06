from modbus_acquire.deif import analog_discrete_for_csv, convert_raw


def test_convert_scales_and_runhours() -> None:
    raw = {
        "Fgen": 5000,
        "Fbus": 5000,
        "Usupply": 230,
        "IL1": 10,
        "IL2": 20,
        "IL3": 30,
        "PF": 8500,
        "Gov.Reg.Value": 500,
        "AVR Reg.Value": 600,
        "Pgen": 1,
        "Qgen": 2,
        "Sgen": 3,
        "PT100_1": 40,
        "PT100_2": 41,
        "Runhours_raw83": 3,
        "Runhours_raw84": 2,
        "AlarmReg_20": 0,
        "AlarmReg_21": 0,
        "AlarmReg_22": 0,
        "AlarmReg_23": 0,
        "AlarmReg_26": 0,
        "AlarmReg_70": 0,
        "AlarmReg_71": 0,
        "AlarmReg_72": 0,
        "AlarmReg_73": 0,
        "AlarmReg_74": 0,
        "AlarmReg_79": 0,
    }
    data = convert_raw(raw)
    assert data["Fgen"] == 50.0
    assert data["Runhours_hours"] == 2003
    analog, discrete = analog_discrete_for_csv(data)
    assert analog["Fgen"] == 50.0
    assert isinstance(discrete["Engine_running"], bool)
