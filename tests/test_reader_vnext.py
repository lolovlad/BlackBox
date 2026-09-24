from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from bb_platform.contracts import Quality, RawBatch, RawSample, VmProtocol
from bb_platform.parser import adapt_legacy_map, diagnose_read, field_channel, parse_batch, split_channels
from services.hub.vm_config import normalize_runtime_config
from workers.modbus_rtu.main import ModbusReader
from workers.modbus_tcp.main import ModbusTcpReader


def test_legacy_reader_settings_are_normalized_per_vm():
    config = normalize_runtime_config(
        {
            "MODBUS_PORT": "/dev/ttyUSB0",
            "MODBUS_SLAVE": "7",
            "MODBUS_BAUDRATE": "19200",
            "MODBUS_INTERVAL": "0.25",
            "MODBUS_TIMEOUT": "0.8",
            "RAM_BATCH_SIZE": "120",
            "storage_target_id": "storage:ssd",
        },
        protocol="modbus_rtu",
    )
    assert config["reader"]["port"] == "/dev/ttyUSB0"
    assert config["reader"]["slave_id"] == 7
    assert config["reader"]["baudrate"] == 19200
    assert config["reader"]["poll_interval_sec"] == 0.25
    assert config["buffer"]["ram_rows"] == 120
    assert config["storage"]["target_resource_id"] == "storage:ssd"


def test_can_reader_settings_are_normalized_per_vm():
    config = normalize_runtime_config(
        {"can_interface": "can1", "bitrate": 500000, "poll_interval_sec": 0.5},
        protocol="can",
    )
    assert config["reader"]["can_interface"] == "can1"
    assert config["reader"]["can_bitrate"] == 500000
    assert config["reader"]["poll_interval_sec"] == 0.5


def test_legacy_storage_flags_are_retained_and_unsafe_subdir_rejected():
    config = normalize_runtime_config(
        {
            "disable_modbus_collector": "0",
            "data_directory": "./data",
            "alarm_directory": "./alarms",
            "data_format": "csv",
        },
        protocol="modbus_rtu",
    )
    assert config["reader"]["enabled"] is True
    assert config["storage"]["legacy_data_directory"] == "./data"
    assert config["storage"]["legacy_alarm_directory"] == "./alarms"
    assert config["storage"]["legacy_data_format"] == "csv"
    assert config["storage"]["data_format"] == "parquet"
    with pytest.raises(ValueError):
        normalize_runtime_config({"storage": {"telemetry_subdir": "../outside"}})


def test_worker_quality_is_preserved_by_parser():
    vm_id = uuid4()
    batch = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.SIMULATOR,
        map_version="map-v1",
        seq_start=1,
        samples=[RawSample(seq=1, captured_at="2026-09-17T00:00:00Z", sources={"sim": [1]}, quality=Quality.BAD)],
    )
    document = adapt_legacy_map(
        {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "x", "type": "uint16", "source": "sim", "address": 0}]},
        protocol=VmProtocol.SIMULATOR,
        version="map-v1",
    )
    parsed = parse_batch(batch, document)[0]
    assert parsed.quality == Quality.BAD
    assert parsed.analog == {}
    assert parsed.discrete == {}
    assert parsed.alerts == []


def test_adapt_legacy_map_keeps_extra_keys_and_unknown_types():
    document = adapt_legacy_map(
        {
            "site": "cabinet-A",
            "requests": [{"name": "hr", "fc": 3, "address": 0, "count": 2, "bus": "rs485"}],
            "fields": [
                {"name": "Ugen", "type": "uint16", "source": "hr", "address": 0, "unit": "V"},
                {"name": "mystery", "type": "float32", "source": "hr", "address": 1},
                {"name": "orphan", "type": "uint16", "source": "missing", "address": 0},
            ],
        },
        protocol=VmProtocol.MODBUS_RTU,
        version="agc-v1",
    )
    assert document.metadata["extra"]["site"] == "cabinet-A"
    assert document.requests[0]["bus"] == "rs485"
    assert document.fields[0]["unit"] == "V"
    names = [field["name"] for field in document.fields]
    assert names == ["Ugen", "mystery"]
    assert document.fields[1]["type"] == "uint16"
    assert any("float32" in item for item in document.metadata["adapt_warnings"])
    assert any("orphan" in item for item in document.metadata["adapt_warnings"])


def test_modbus_tcp_reader_reads_registers_from_fake_server():
    ready = threading.Event()
    stop = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        ready.set()
        conn, _ = server.accept()
        with conn:
            header = conn.recv(7)
            tid, protocol, length, unit = struct.unpack(">HHHB", header)
            assert protocol == 0 and unit == 1 and length == 6
            pdu = conn.recv(length - 1)
            function, address, count = struct.unpack(">BHH", pdu)
            assert function == 3 and address == 10 and count == 2
            payload = struct.pack(">BBHH", function, 4, 123, 456)
            conn.sendall(struct.pack(">HHHB", tid, 0, len(payload) + 1, 1) + payload)
        stop.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(1)
    try:
        reader = ModbusTcpReader("127.0.0.1", port, timeout=1, retries=1)
        assert reader.read([{"name": "holding", "fc": 3, "address": 10, "count": 2}]) == {"holding": [123, 456]}
    finally:
        server.close()
        thread.join(timeout=1)
    assert stop.is_set()


def test_modbus_tcp_reader_applies_legacy_address_offset():
    reader = ModbusTcpReader.__new__(ModbusTcpReader)
    reader.address_offset = 0
    reader.retries = 1
    reader.retry_delay = 0
    calls = []
    reader._request = lambda function, address, count: calls.append((function, address, count)) or [1]  # type: ignore[method-assign]
    assert reader.read([{"name": "holding", "fc": 3, "address": 10, "count": 1}]) == {"holding": [1]}
    assert calls == [(3, 9, 1)]


def test_field_channel_follows_legacy_registration():
    assert field_channel({"name": "RPM", "type": "uint16"}) == "analog"
    assert field_channel({"name": "Engine_running", "type": "bool"}) == "discrete"
    assert field_channel({"name": "flag", "type": "uint16", "bit": 0}) == "discrete"
    assert field_channel({"name": "active_alarms", "type": "bitfield"}) == "alert"
    assert field_channel({"name": "Warning", "type": "bool", "kind": "discrete"}) == "discrete"
    assert field_channel({"name": "x", "type": "uint16", "kind": "alert"}) == "alert"


def test_split_channels_keeps_alerts_out_of_analog():
    analog, discrete, alerts = split_channels(
        [
            {"name": "RPM", "type": "uint16"},
            {"name": "Engine_running", "type": "bool"},
            {"name": "active_alarms", "type": "bitfield"},
        ],
        {"RPM": 0, "Engine_running": False, "active_alarms": ["Overspeed"]},
    )
    assert analog == {"RPM": 0}
    assert discrete == {"Engine_running": False}
    assert alerts == ["Overspeed"]


def test_diagnose_read_separates_link_from_device_alerts():
    silent = diagnose_read(quality="bad", last_error="No communication with the instrument (no answer)", has_sample=True)
    assert silent["code"] == "no_answer" and silent["can_read_alerts"] is False
    zeros = diagnose_read(quality="good", alerts=[], has_sample=True)
    assert zeros["code"] == "ok" and zeros["can_read_alerts"] is True
    device = diagnose_read(quality="good", alerts=["Overspeed"], has_sample=True)
    assert device["code"] == "device_alerts"
    leftover = diagnose_read(
        quality="good",
        last_error="No communication with the instrument (no answer)",
        alerts=[],
        has_sample=True,
    )
    assert leftover["code"] == "ok"
    port = diagnose_read(quality="bad", last_error="[Errno 2] could not open port /dev/ttyAMA10", has_sample=True)
    assert port["cause"] == "port"
    missing = diagnose_read(quality=None, last_error="", has_sample=False)
    assert missing["code"] == "no_sample"
    error_before_sample = diagnose_read(
        quality=None,
        last_error="Modbus request hr failed after 3 retries — No communication with the instrument (no answer)",
        has_sample=False,
    )
    assert error_before_sample["code"] == "no_answer"


def test_parse_batch_splits_channels_and_blanks_them_on_bad_quality():
    document = adapt_legacy_map(
        {
            "requests": [{"name": "hr", "fc": 3, "address": 0, "count": 20}],
            "fields": [
                {"name": "RPM", "type": "uint16", "source": "hr", "address": 0},
                {"name": "active_alarms", "type": "bitfield", "source": "hr", "address": 19, "bits": {"0": "Overspeed"}},
            ],
        },
        protocol=VmProtocol.SIMULATOR,
        version="map-v1",
    )
    holding = [0] * 20
    holding[0] = 90
    holding[19] = 1
    vm_id = uuid4()
    good = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.SIMULATOR,
        map_version="map-v1",
        seq_start=1,
        samples=[RawSample(seq=1, captured_at="2026-09-17T00:00:00Z", sources={"hr": holding})],
    )
    parsed = parse_batch(good, document)[0]
    assert parsed.analog["RPM"] == 90
    assert parsed.alerts == ["Overspeed"]
    bad = RawBatch(
        vm_id=vm_id,
        protocol=VmProtocol.SIMULATOR,
        map_version="map-v1",
        seq_start=2,
        samples=[RawSample(seq=2, captured_at="2026-09-17T00:00:01Z", sources={}, quality=Quality.BAD)],
    )
    lost = parse_batch(bad, document)[0]
    assert lost.quality == Quality.BAD
    assert lost.analog == {}
    assert lost.alerts == []


def test_modbus_rtu_reader_keeps_other_requests_on_partial_failure():
    class FakeInstrument:
        def read_registers(self, address, count, functioncode=3):
            raise OSError("No communication with the instrument (no answer)")

        def read_bits(self, address, count, functioncode):
            return [1, 0]

    reader = ModbusReader(FakeInstrument(), retries=1, retry_delay=0)
    sources = reader.read(
        [
            {"name": "hr", "fc": 3, "address": 0, "count": 1},
            {"name": "coils", "fc": 1, "address": 0, "count": 2},
        ]
    )
    assert sources["hr"] == []
    assert sources["coils"] == [True, False]
    assert reader.last_failures[0][0] == "hr"


def test_modbus_rtu_reader_raises_when_every_request_fails():
    class FakeInstrument:
        def read_registers(self, address, count, functioncode=3):
            raise OSError("No communication with the instrument (no answer)")

        def read_bits(self, address, count, functioncode):
            raise OSError("No communication with the instrument (no answer)")

    reader = ModbusReader(FakeInstrument(), retries=1, retry_delay=0)
    with pytest.raises(RuntimeError, match="hr"):
        reader.read([{"name": "hr", "fc": 3, "address": 0, "count": 1}])


def test_modbus_tcp_reader_keeps_other_requests_on_partial_failure():
    reader = ModbusTcpReader.__new__(ModbusTcpReader)
    reader.address_offset = 1
    reader.retries = 1
    reader.retry_delay = 0

    def _request(function, address, count):
        if function == 3:
            raise OSError("No communication with the instrument (no answer)")
        return [1, 0]

    reader._request = _request  # type: ignore[method-assign]
    sources = reader.read(
        [
            {"name": "hr", "fc": 3, "address": 0, "count": 1},
            {"name": "coils", "fc": 1, "address": 0, "count": 2},
        ]
    )
    assert sources["hr"] == []
    assert sources["coils"] == [1, 0]
    assert reader.last_failures[0][0] == "hr"
