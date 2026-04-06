from modbus_acquire import deif
from modbus_acquire import instrument as modbus_instrument


class _FakeSerial:
    def __init__(self):
        self.baudrate = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.timeout = None


class _FakeInstrumentCtor:
    def __init__(self, port, slave_id, mode):
        self.port = port
        self.slave_id = slave_id
        self.mode = mode
        self.serial = _FakeSerial()
        self.close_port_after_each_call = None
        self.clear_buffers_before_each_transaction = None


def test_build_instrument_uses_configured_port(monkeypatch):
    monkeypatch.setattr(modbus_instrument.minimalmodbus, "Instrument", _FakeInstrumentCtor)
    inst = modbus_instrument.build_instrument(
        {
            "port": "/dev/ttyUSB9",
            "slave_id": 7,
            "baudrate": 19200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": 0.55,
            "close_port_after_each_call": True,
            "clear_buffers_before_each_transaction": False,
        }
    )
    assert inst.port == "/dev/ttyUSB9"
    assert inst.slave_id == 7
    assert inst.serial.baudrate == 19200
    assert inst.serial.timeout == 0.55
    assert inst.close_port_after_each_call is True
    assert inst.clear_buffers_before_each_transaction is False


class _PollInstrument:
    def __init__(self):
        self.calls = []

    def read_registers(self, address, count):
        self.calls.append(("regs", address, count))
        regs = [0] * 90
        regs[0] = 400
        regs[6] = 5000
        regs[82] = 3
        regs[83] = 2
        return regs

    def read_bits(self, address, count, functioncode):
        self.calls.append(("bits", address, count, functioncode))
        bits = [False] * 32
        bits[0] = True
        return bits


def test_poll_raw_reads_holding_and_coils():
    inst = _PollInstrument()
    raw = deif.poll_raw(inst, address_offset=1)
    assert ("regs", 1, 90) in inst.calls
    assert ("bits", 16, 32, 1) in inst.calls
    assert "UgenL1L2" in raw and isinstance(raw["UgenL1L2"], int)
    assert "Fgen" in raw and isinstance(raw["Fgen"], int)
    assert "Engine_running" in raw and isinstance(raw["Engine_running"], bool)
