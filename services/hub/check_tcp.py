"""Layered Modbus TCP check: DNS → ICMP → TCP port → FC3 holding read.

Same idea as ``check_read`` for UART: stop at the first broken layer so the
operator sees whether the fault is name resolution, ICMP, the TCP port, or
Modbus itself.

Run on the stand, inside Hub::

    ./bbctl check-tcp --host 192.168.1.10
    ./bbctl check-tcp --host 10.109.114.1 --port 502 --unit 1 --full
"""

from __future__ import annotations

import argparse
import sys
import time

from .link import classify_tcp_error, icmp_ping, resolve_host, tcp_rtt



def _print(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def check_host(
    host: str,
    *,
    port: int = 502,
    unit: int = 1,
    timeout: float = 0.8,
    address_offset: int = 1,
    count: int = 10,
) -> int:
    host = str(host or "").strip()
    _print(f"1) Host: {host or '—'}")
    if not host:
        _print("   Укажите IP или hostname: ./bbctl check-tcp --host 192.168.1.10")
        return 2
    resolved = resolve_host(host)
    if not resolved["ok"]:
        code, hint = classify_tcp_error(OSError(resolved.get("error") or "dns"))
        if "name" not in str(resolved.get("error") or "").lower() and "dns" not in code:
            code, hint = "dns", "Имя хоста не резолвится. Проверьте IP/DNS и что Hub видит эту сеть."
        _print(f"   DNS НЕ резолвится [{code}]: {resolved.get('error')}")
        _print(f"   {hint}")
        return 3
    _print(f"   DNS: {resolved['address']}")

    ping = icmp_ping(host, timeout=max(1.0, timeout))
    if ping.get("ok"):
        _print(f"2) ICMP ping: {ping['rtt_ms']} мс")
    elif ping.get("error") == "ping_missing":
        _print("2) ICMP ping: нет утилиты ping в контейнере Hub. Дальше проверяем TCP-порт.")
    else:
        _print(f"2) ICMP ping: нет ответа ({ping.get('error') or 'timeout'}). Хост может фильтровать ICMP — это ещё не приговор.")

    handshake = tcp_rtt(host, port, timeout=timeout)
    endpoint = f"{host}:{port}"
    if handshake.get("ok"):
        _print(f"3) TCP {endpoint}: открылся за {handshake['rtt_ms']} мс")
    else:
        code = str(handshake.get("error") or "timeout")
        _, hint = classify_tcp_error(OSError(handshake.get("detail") or code))
        if code == "refused":
            hint = "Хост отвечает, но порт закрыт. Modbus TCP обычно 502."
        elif code == "timeout":
            hint = "Нет ответа на TCP. Другой VLAN, хост выключен, или фильтр режет 502."
        elif code == "network":
            hint = "Сеть недоступна с хоста Hub."
        _print(f"3) TCP {endpoint}: НЕ открылся [{code}] за {handshake.get('rtt_ms')} мс")
        _print(f"   {hint}")
        return 3

    from workers.modbus_tcp.main import ModbusTcpReader

    reader = ModbusTcpReader(
        host,
        int(port),
        int(unit),
        timeout=timeout,
        retries=1,
        retry_delay=0,
        address_offset=address_offset,
    )
    requests = [{"name": "hr", "fc": 3, "address": 1, "count": count}]
    t0 = time.perf_counter()
    try:
        sources = reader.read(requests)
        elapsed = time.perf_counter() - t0
        regs = sources.get("hr") or []
        start = int(1) + int(address_offset) - 1
        _print(f"4) Holding FC3 unit={unit} addr={start} count={count}: {len(regs)} регистров за {elapsed:.3f} с")
        _print(f"   Сырые значения: {regs[:16]}{' …' if len(regs) > 16 else ''}")
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        code, hint = classify_tcp_error(exc)
        _print(f"4) Holding НЕ прочитался за {elapsed:.3f} с [{code}]: {exc}")
        _print(f"   {hint}")
        return 4

    _print("OK: прибор отвечает по Modbus TCP. Если ВМ всё равно ругается — смотрите Unit ID и карту (fc/address/count).")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка, что с IP вообще читается Modbus TCP")
    parser.add_argument("--host", help="IP или hostname прибора")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=1, help="Modbus Unit ID")
    parser.add_argument("--timeout", type=float, default=0.8)
    parser.add_argument("--address-offset", type=int, default=1, dest="address_offset")
    parser.add_argument("--count", type=int, default=10, help="Сколько holding-регистров прочитать")
    parser.add_argument("--full", action="store_true", help="90 holding, как в RTU check-read --full")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    count = 90 if args.full else args.count
    return check_host(
        args.host or "",
        port=args.port,
        unit=args.unit,
        timeout=args.timeout,
        address_offset=args.address_offset,
        count=count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
