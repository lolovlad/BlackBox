"""Dependency-light Modbus TCP worker.

Using a small socket client keeps the worker image portable on Raspberry Pi
and makes the protocol reader easy to test with a fake instrument/server.
Function codes 1, 2, 3 and 4 are supported, matching the RTU worker.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from typing import Any

from bb_platform.contracts import VmProtocol
from workers.common import WorkerClient


class ModbusTcpReader:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, *, timeout: float = 1.0, retries: int = 3, retry_delay: float = 0.2, address_offset: int = 1) -> None:
        self.host = host
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.timeout = max(0.01, float(timeout))
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.address_offset = int(address_offset)
        self._transaction_id = 0

    def _request(self, function: int, address: int, count: int) -> list[Any]:
        if function not in {1, 2, 3, 4}:
            raise ValueError(f"Unsupported Modbus function code: {function}")
        max_count = 0x7D0 if function in {1, 2} else 0x7D
        if not 0 <= address <= 0xFFFF or address + count > 0x10000 or not 1 <= count <= max_count:
            raise ValueError("Modbus address/count is outside the valid range")
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        pdu = struct.pack(">BHH", function, address, count)
        # Protocol identifier 0, length = unit id + PDU (7 bytes).
        packet = struct.pack(">HHHB", self._transaction_id, 0, len(pdu) + 1, self.unit_id) + pdu
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(packet)
            header = self._recv_exact(sock, 7)
            tid, protocol, length, unit = struct.unpack(">HHHB", header)
            if tid != self._transaction_id or protocol != 0 or unit != self.unit_id or length < 2:
                raise RuntimeError("Invalid Modbus TCP response header")
            body = self._recv_exact(sock, length - 1)
        response_fc = body[0]
        if response_fc == function | 0x80:
            code = body[1] if len(body) > 1 else 0
            raise RuntimeError(f"Modbus exception response: {code}")
        if response_fc != function or len(body) < 2:
            raise RuntimeError("Invalid Modbus TCP response function")
        byte_count = body[1]
        payload = body[2:]
        if byte_count != len(payload):
            raise RuntimeError("Invalid Modbus TCP byte count")
        if function in {1, 2}:
            if byte_count * 8 < count:
                raise RuntimeError("Modbus TCP bit response is too short")
            return [bool((payload[index // 8] >> (index % 8)) & 1) for index in range(count)]
        if byte_count != count * 2:
            raise RuntimeError("Modbus TCP register response has an unexpected length")
        return [struct.unpack_from(">H", payload, index * 2)[0] for index in range(count)]

    @staticmethod
    def _recv_exact(sock: socket.socket, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise OSError("Modbus TCP peer closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read(self, requests: list[dict[str, Any]]) -> dict[str, list[Any]]:
        output: dict[str, list[Any]] = {}
        for request in requests:
            name = str(request.get("name", "holding"))
            function = int(request.get("fc", 3))
            if function not in {1, 2, 3, 4}:
                raise ValueError(f"Unsupported Modbus function code: {function}")
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    map_address = int(request.get("address", 0))
                    output[name] = self._request(function, map_address + self.address_offset - 1, int(request.get("count", 1)))
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.retries:
                        time.sleep(self.retry_delay)
            else:
                raise RuntimeError(f"Modbus TCP request {name} failed after {self.retries} retries") from last_error
        return output


def _reader_config(config: dict[str, Any]) -> dict[str, Any]:
    reader = config.get("reader") if isinstance(config.get("reader"), dict) else config
    return dict(reader or {})


def run() -> int:
    client = WorkerClient(protocol=VmProtocol.MODBUS_TCP)
    raw = json.loads(os.getenv("BB_PROTOCOL_CONFIG", "{}"))
    config = raw if isinstance(raw, dict) else {}
    map_version = os.getenv("BB_MAP_VERSION", "default-v1")
    requests = list(config.get("requests") or [])
    while True:
        try:
            client.register()
            break
        except Exception:
            time.sleep(1)
    try:
        current = client.configuration()
        config = dict(current.get("config") or config)
        map_version = str(current.get("map_version") or map_version)
        requests = list((current.get("map") or {}).get("requests") or requests)
    except Exception:
        pass
    reader: ModbusTcpReader | None = None
    last_error = ""
    while True:
        try:
            commands = client.commands()
        except Exception as exc:
            if str(exc) != last_error:
                try:
                    client.report_error("hub_unavailable", str(exc))
                except Exception:
                    pass
                last_error = str(exc)
            time.sleep(1)
            continue
        for command in commands:
            action = str(command.get("action", ""))
            if action == "stop":
                return 0
            if action in {"restart", "apply_map", "apply_config"}:
                try:
                    current = client.configuration()
                    config = dict(current.get("config") or config)
                    map_version = str(current.get("map_version") or map_version)
                    requests = list((current.get("map") or {}).get("requests") or requests)
                    reader = None
                    client.acknowledge(command)
                except Exception as exc:
                    client.acknowledge(command, accepted=False, message=str(exc))
                    client.report_error("config_apply_failed", str(exc))
                continue
            client.acknowledge(command)
        if reader is None:
            cfg = _reader_config(config)
            if not cfg.get("enabled", True):
                try:
                    client.heartbeat()
                except Exception:
                    pass
                time.sleep(float(cfg.get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))
                continue
            reader = ModbusTcpReader(
                str(cfg.get("host", "127.0.0.1")),
                int(cfg.get("tcp_port", cfg.get("port", 502))),
                int(cfg.get("unit_id", cfg.get("slave_id", 1))),
                timeout=float(cfg.get("timeout_sec", cfg.get("timeout", 1.0))),
                retries=int(cfg.get("retries", 3)),
                retry_delay=float(cfg.get("retry_delay_sec", 0.2)),
                address_offset=int(cfg.get("address_offset", 1)),
            )
        try:
            client.batch(reader.read(requests or [{"name": "holding", "fc": 3, "address": 0, "count": 1}]), map_version)
            last_error = ""
        except Exception as exc:
            if str(exc) != last_error:
                try:
                    client.report_error("read_failed", str(exc))
                except Exception:
                    pass
                last_error = str(exc)
            try:
                client.batch({}, map_version, quality="bad")
            except Exception:
                pass
        try:
            client.heartbeat()
        except Exception:
            pass
        time.sleep(float(_reader_config(config).get("poll_interval_sec", os.getenv("BB_INTERVAL", "1"))))


if __name__ == "__main__":
    raise SystemExit(run())
