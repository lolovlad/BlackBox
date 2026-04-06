from modbus_acquire import instrument as modbus_reader


class _FakeInstrument:
    def __init__(self):
        self.calls = []
        self.serial = type("Serial", (), {})()

    def read_register(self, registeraddress, number_of_decimals, functioncode, signed):
        self.calls.append(("reg", registeraddress, functioncode, signed))
        values = {
            0: 2304,
            1: 2311,
            2: 2298,
            3: 5000,
            4: 1500,
            20: 0b00000101,
        }
        return values[registeraddress]

    def read_long(self, registeraddress, functioncode, signed, byteorder):
        self.calls.append(("long", registeraddress, functioncode, signed, byteorder))
        if registeraddress == 10:
            return 1234
        raise ValueError("unexpected address")


def test_read_all_data_scaling_32bit_and_alarms(monkeypatch):
    fake = _FakeInstrument()
    monkeypatch.setattr(modbus_reader, "_build_instrument", lambda cfg: fake)

    data = modbus_reader.read_all_data(
        {
            "retry_count": 1,
            "retry_delay_sec": 0.0,
        }
    )

    assert data["voltage_L1"] == 230.4
    assert data["voltage_L2"] == 231.1
    assert data["voltage_L3"] == 229.8
    assert data["frequency"] == 50.0
    assert data["engine_rpm"] == 1500
    assert data["power"] == 123.4
    assert "low_oil_pressure" in data["alarms"]
    assert "overspeed" in data["alarms"]


def test_read_with_retries_success_on_second_attempt():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise IOError("temporary error")
        return 42

    result = modbus_reader._read_with_retries(flaky, retries=3, delay_sec=0.0)
    assert result == 42
    assert attempts["count"] == 2


def test_read_all_data_custom_fields_and_raw(monkeypatch):
    fake = _FakeInstrument()
    monkeypatch.setattr(modbus_reader, "_build_instrument", lambda cfg: fake)

    data = modbus_reader.read_all_data(
        {
            "retry_count": 1,
            "retry_delay_sec": 0.0,
            "include_raw": True,
            "fields": [
                {
                    "name": "hz",
                    "address": 3,
                    "reg_type": "input",
                    "data_type": "u16",
                    "scale": 0.01,
                },
                {
                    "name": "alarm_bits",
                    "address": 20,
                    "reg_type": "input",
                    "data_type": "bitfield",
                    "bit_labels": {0: "oil", 2: "speed"},
                },
            ],
        }
    )

    assert data["hz"] == 50.0
    assert data["alarm_bits"] == ["oil", "speed"]
    assert data["_raw"]["hz"] == 5000
