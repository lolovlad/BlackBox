from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from bb_platform.contracts import Quality, RawBatch, RawSample, VmProtocol
from bb_platform.parser import parse_batch
from services.hub.vm_config import normalize_runtime_config
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
    from bb_platform.parser import adapt_legacy_map

    document = adapt_legacy_map(
        {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "x", "type": "uint16", "source": "sim", "address": 0}]},
        protocol=VmProtocol.SIMULATOR,
        version="map-v1",
    )
    assert parse_batch(batch, document)[0].quality == Quality.BAD


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
