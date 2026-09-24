from __future__ import annotations

import socket
from pathlib import Path

from services.hub.check_tcp import check_host
from services.hub.link import classify_tcp_error, parse_icmp_rtt, tcp_rtt, vm_link_status


def test_parse_icmp_rtt_linux_and_windows():
    assert parse_icmp_rtt("64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=12.4 ms") == 12.4
    assert parse_icmp_rtt("Reply from 10.0.0.1: bytes=32 time=8ms TTL=128") == 8.0
    assert parse_icmp_rtt("Reply from 10.0.0.1: bytes=32 time<1ms TTL=128") == 1.0
    assert parse_icmp_rtt("no rtt here") is None


def test_classify_tcp_layers():
    assert classify_tcp_error(socket.gaierror(8, "Name or service not known"))[0] == "dns"
    refused = ConnectionRefusedError("Connection refused")
    refused.errno = 111
    assert classify_tcp_error(refused)[0] == "refused"
    timeout = TimeoutError("timed out")
    assert classify_tcp_error(timeout)[0] == "timeout"
    assert classify_tcp_error(RuntimeError("Modbus exception response: 2"))[0] == "exception"


def test_tcp_rtt_reports_refused(monkeypatch):
    def opener(addr, timeout=0):
        raise ConnectionRefusedError("Connection refused")

    result = tcp_rtt("127.0.0.1", 9, timeout=0.2, opener=opener)
    assert result["ok"] is False
    assert result["error"] == "refused"
    assert result["method"] == "tcp"


def test_vm_link_status_tcp_uses_measured_ping(monkeypatch):
    from services.hub import link as link_mod

    monkeypatch.setattr(
        link_mod,
        "measure_tcp_link",
        lambda host, port, **kwargs: {"link": "up", "ping_ms": 7.5, "ping_method": "icmp", "detail": "7.5 мс", "tcp_ok": True},
    )
    item = vm_link_status({"id": "vm-1", "protocol": "modbus_tcp", "config": {"reader": {"host": "10.0.0.8", "tcp_port": 502}}})
    assert item["shows_link"] is True
    assert item["shows_ping"] is True
    assert item["link"] == "up"
    assert item["ping_ms"] == 7.5


def test_vm_link_status_rtu_has_dot_without_ping():
    item = vm_link_status({"id": "vm-2", "protocol": "modbus_rtu", "last_error": "", "config": {"reader": {"port": "/dev/ttyAMA0"}}})
    assert item["shows_link"] is True
    assert item["shows_ping"] is False
    assert item["ping_ms"] is None
    assert item["link"] == "unknown"


def test_vm_link_status_simulator_hides_indicator():
    item = vm_link_status({"id": "vm-3", "protocol": "simulator"})
    assert item["shows_link"] is False
    assert item["shows_ping"] is False


def test_check_tcp_cli_is_wired(tmp_path: Path):
    assert "check-tcp" in (Path(__file__).resolve().parent.parent / "bbctl").read_text(encoding="utf-8")
    assert check_host("") == 2
