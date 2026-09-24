"""Live link status for VM cards: serial/CAN from last sample, TCP from ping + port.

ICMP is best-effort (needs ``ping`` and often ``NET_RAW``).  The number shown
in the UI is ICMP RTT when that works, otherwise the TCP handshake time to the
Modbus port — that is the socket the worker actually uses.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from bb_platform.parser import diagnose_read

from .connection import connection_profile

_RTT_RE = re.compile(r"(?:time|время)\s*[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 3.0


def classify_tcp_error(exc: BaseException) -> tuple[str, str]:
    parts = [str(exc or "")]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    text = " ".join(part for part in parts if part)
    lowered = text.lower()
    errno = getattr(exc, "errno", None) or getattr(cause, "errno", None) if cause is not None else getattr(exc, "errno", None)
    if isinstance(exc, socket.gaierror) or "name or service not known" in lowered or "getaddrinfo" in lowered or "nodename nor servname" in lowered:
        return "dns", "Имя хоста не резолвится. Проверьте IP/DNS и что Hub видит эту сеть."
    if errno in {113, 101} or "network is unreachable" in lowered or "no route" in lowered:
        return "network", "Сеть недоступна с хоста Hub. Проверьте маршрут, VLAN и что прибор в той же сети."
    if isinstance(exc, ConnectionRefusedError) or errno in {111} or "connection refused" in lowered:
        return "refused", "Хост отвечает, но порт закрыт. Modbus TCP обычно 502: служба не слушает или фильтр режет вход."
    if isinstance(exc, (TimeoutError, socket.timeout)) or errno in {110, 11} or "timed out" in lowered or "timeout" in lowered:
        return "timeout", "Нет ответа на TCP. Хост выключен, другой VLAN, или ICMP/SYN фильтруется."
    if "modbus exception" in lowered:
        return "exception", "TCP-сессия открылась, прибор вернул Modbus exception. Проверьте Unit ID и карту."
    if "invalid modbus tcp" in lowered or "peer closed" in lowered:
        return "protocol", "Порт открылся, но это не Modbus TCP. Проверьте порт и что на нём именно прибор."
    return "error", text or type(exc).__name__


def parse_icmp_rtt(output: str) -> float | None:
    match = _RTT_RE.search(output or "")
    if not match:
        return None
    try:
        return round(float(match.group(1)), 1)
    except ValueError:
        return None


def icmp_ping(
    host: str,
    *,
    timeout: float = 1.0,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    runner = run or subprocess.run
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(max(1, int(timeout * 1000))), host]
    else:
        command = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=timeout + 1.5)
    except FileNotFoundError:
        return {"ok": False, "rtt_ms": None, "error": "ping_missing", "method": "icmp"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rtt_ms": None, "error": "timeout", "method": "icmp"}
    except Exception as exc:
        return {"ok": False, "rtt_ms": None, "error": str(exc), "method": "icmp"}
    blob = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    rtt = parse_icmp_rtt(blob)
    ok = completed.returncode == 0 and rtt is not None
    return {"ok": ok, "rtt_ms": rtt, "error": None if ok else (blob.strip()[:180] or f"exit {completed.returncode}"), "method": "icmp"}


def tcp_rtt(
    host: str,
    port: int,
    *,
    timeout: float = 0.8,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    connect = opener or socket.create_connection
    t0 = time.perf_counter()
    try:
        with connect((host, int(port)), timeout=timeout):
            elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
        return {"ok": True, "rtt_ms": elapsed, "error": None, "method": "tcp"}
    except Exception as exc:
        elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
        code, _hint = classify_tcp_error(exc)
        return {"ok": False, "rtt_ms": elapsed, "error": code, "detail": str(exc), "method": "tcp"}


def resolve_host(host: str) -> dict[str, Any]:
    text = str(host or "").strip()
    if not text:
        return {"ok": False, "host": "", "address": None, "error": "empty"}
    try:
        infos = socket.getaddrinfo(text, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {"ok": False, "host": text, "address": None, "error": str(exc)}
    address = infos[0][4][0] if infos else None
    return {"ok": bool(address), "host": text, "address": address, "error": None if address else "empty"}


def measure_tcp_link(
    host: str,
    port: int,
    *,
    timeout: float = 0.8,
    use_cache: bool = True,
    icmp: Callable[..., dict[str, Any]] | None = None,
    handshake: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = (str(host).strip(), int(port))
    now = time.monotonic()
    if use_cache:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SEC:
            return dict(cached[1])
    ping = (icmp or icmp_ping)(key[0], timeout=timeout)
    tcp = (handshake or tcp_rtt)(key[0], key[1], timeout=timeout)
    if ping.get("ok") and ping.get("rtt_ms") is not None:
        ping_ms = ping["rtt_ms"]
        method = "icmp"
    elif tcp.get("ok") and tcp.get("rtt_ms") is not None:
        ping_ms = tcp["rtt_ms"]
        method = "tcp"
    else:
        ping_ms = None
        method = "icmp" if ping.get("error") not in {None, "ping_missing"} else "tcp"
    if tcp.get("ok"):
        link = "up"
        detail = f"{ping_ms} мс" if ping_ms is not None else "TCP-порт открыт"
    else:
        link = "down"
        code = str(tcp.get("error") or "timeout")
        detail = {
            "dns": "DNS не резолвится",
            "network": "Сеть недоступна",
            "refused": "Порт закрыт",
            "timeout": "Нет ответа",
        }.get(code, "Нет TCP")
    payload = {
        "link": link,
        "ping_ms": ping_ms,
        "ping_method": method,
        "ping_ok": bool(ping.get("ok")),
        "tcp_ok": bool(tcp.get("ok")),
        "detail": detail,
        "icmp": ping,
        "tcp": tcp,
    }
    if use_cache:
        _CACHE[key] = (now, payload)
    return dict(payload)


def _reader(vm: dict[str, Any]) -> dict[str, Any]:
    config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
    reader = config.get("reader") if isinstance(config.get("reader"), dict) else {}
    return reader


def _can_operstate(iface: str) -> str:
    env_root = os.getenv("BB_DISCOVERY_SYS_CLASS_NET", "").strip()
    root = Path(env_root) if env_root else Path("/host/sys/class/net" if Path("/host/sys/class/net").exists() else "/sys/class/net")
    if not iface:
        return "missing"
    node = root / iface
    if not node.exists():
        return "missing"
    try:
        return (node / "operstate").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def vm_link_status(vm: dict[str, Any], sample: Any = None) -> dict[str, Any]:
    protocol = str(vm.get("protocol") or "")
    profile = connection_profile(protocol)
    item = {
        "vm_id": str(vm.get("id") or ""),
        "protocol": protocol,
        "shows_link": profile.link in {"serial", "network", "can"},
        "shows_ping": profile.link == "network",
        "link": "unknown",
        "ping_ms": None,
        "ping_method": None,
        "detail": "",
    }
    if not item["shows_link"]:
        return item
    reader = _reader(vm)
    if profile.link == "network":
        host = str(reader.get("host") or "").strip()
        port = int(reader.get("tcp_port") or reader.get("port") or 502)
        if not host:
            item["link"] = "down"
            item["detail"] = "IP не задан"
            return item
        measured = measure_tcp_link(host, port)
        item["link"] = measured["link"]
        item["ping_ms"] = measured["ping_ms"]
        item["ping_method"] = measured["ping_method"]
        item["detail"] = measured["detail"]
        return item
    if profile.link == "can":
        iface = str(reader.get("can_interface") or "").strip()
        state = _can_operstate(iface)
        if state == "up":
            item["link"] = "up"
            item["detail"] = f"{iface} up"
        elif state == "missing":
            item["link"] = "down"
            item["detail"] = f"{iface or 'CAN'} не найден"
        else:
            item["link"] = "down"
            item["detail"] = f"{iface} {state}"
        return item
    quality = getattr(getattr(sample, "quality", None), "value", getattr(sample, "quality", None))
    diagnosis = diagnose_read(quality=quality, last_error=vm.get("last_error"), has_sample=sample is not None)
    item["link"] = str(diagnosis.get("link") or "unknown")
    item["detail"] = str(diagnosis.get("title") or "")
    return item


__all__ = [
    "classify_tcp_error",
    "icmp_ping",
    "measure_tcp_link",
    "parse_icmp_rtt",
    "resolve_host",
    "tcp_rtt",
    "vm_link_status",
]
