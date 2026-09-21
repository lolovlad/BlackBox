"""Layered Modbus RTU check: node → open port → FC3 holding read.

Uses the same serial settings and holding-block as legacy
``legase/modbus_opt_v3.py`` (RTU, 9600 8N1, timeout 0.35, address_offset=1,
read 90 holdings starting at register 1).

Run on the stand, inside Hub (this is the path the VM actually uses)::

    ./bbctl check-read --port /dev/ttyAMA10
    ./bbctl check-read --port /dev/ttyAMA10 --full --coils
"""

from __future__ import annotations

import argparse
import stat
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from services.hub.discovery import hub_serial_path, operator_serial_path


SERIAL_GLOBS = ("ttyAMA*", "ttyUSB*", "ttyACM*", "ttyS*", "serial0", "serial1")


def _print(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def list_serial_nodes(roots: Iterable[Path] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    search = list(roots) if roots is not None else [Path("/host-dev"), Path("/dev")]
    for root in search:
        if not root.is_dir():
            continue
        for pattern in SERIAL_GLOBS:
            for path in sorted(root.glob(pattern)):
                key = path.name
                if key in seen:
                    continue
                try:
                    if path.exists():
                        seen.add(key)
                        found.append(path)
                except OSError:
                    continue
    return found


def pdu_address(map_address: int, address_offset: int) -> int:
    return int(map_address) + int(address_offset) - 1


def classify_error(exc: BaseException) -> tuple[str, str]:
    parts = [str(exc or "")]
    errno = getattr(exc, "errno", None)
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
        if errno is None:
            errno = getattr(cause, "errno", None)
    text = " ".join(part for part in parts if part)
    lowered = text.lower()
    if errno in {2, 6} or "could not open port" in lowered or "no such file" in lowered:
        return "port_missing", "Узел порта нет или это не serial-устройство. Проверьте путь и монтирование /dev в контейнер."
    if errno in {13} or "permission" in lowered:
        return "permission", "Нет прав на порт. На хосте: пользователь в группе dialout, либо откройте порт из контейнера Hub."
    if errno in {16} or "busy" in lowered or "exclusive" in lowered:
        return "busy", "Порт занят: остановите ВМ, которая уже держит этот UART, и повторите."
    if "checksum" in lowered:
        return "checksum", "Порт открылся, но ответ битый. Часто: два мастера на шине, неверный baudrate или помехи RS-485."
    if "timed out" in lowered or "timeout" in lowered or "no communication" in lowered or "errno 11" in lowered:
        return "no_answer", "Порт открылся, прибор не ответил. Проверьте Slave ID, baudrate, A/B RS-485 и что на линии есть DEIF."
    return "error", text or type(exc).__name__


def _describe_node(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        kind = "char" if stat.S_ISCHR(mode) else ("link" if path.is_symlink() else "file")
        return f"{path} ({kind} {oct(mode & 0o777)})"
    except OSError as exc:
        return f"{path} (не читается: {exc})"


def _open_instrument(port: str, *, slave: int, baudrate: int, timeout: float) -> Any:
    from workers.modbus_rtu.main import _make_instrument

    return _make_instrument(
        {
            "port": port,
            "slave_id": slave,
            "baudrate": baudrate,
            "timeout_sec": timeout,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "mode": "rtu",
            "close_port_after_each_call": True,
            "clear_buffers_before_each_transaction": True,
        }
    )


def check_port(
    port: str,
    *,
    slave: int = 1,
    baudrate: int = 9600,
    timeout: float = 0.35,
    address_offset: int = 1,
    count: int = 10,
    coils: bool = False,
) -> int:
    in_hub = Path("/host-dev").exists()
    resolved = hub_serial_path(port)
    shown = operator_serial_path(resolved)
    _print(f"1) Порт: {shown}")
    _print(f"   Открываем как: {resolved} ({'контейнер Hub, /host-dev' if in_hub else 'этот хост'})")
    node = Path(resolved)
    if not node.exists():
        _print("   НЕТ узла. Сначала: ./bbctl check-read --list")
        return 2
    _print(f"   Узел: {_describe_node(node)}")

    instrument = None
    try:
        instrument = _open_instrument(resolved, slave=slave, baudrate=baudrate, timeout=timeout)
        serial = instrument.serial
        if not serial.is_open:
            serial.open()
        _print("2) Serial открыт.")
    except Exception as exc:
        code, hint = classify_error(exc)
        _print(f"2) Serial НЕ открылся [{code}]: {exc}")
        _print(f"   {hint}")
        return 3

    from workers.modbus_rtu.main import ModbusReader, _close_instrument

    start = pdu_address(1, address_offset)
    reader = ModbusReader(instrument, retries=3, retry_delay=0.2, address_offset=address_offset)
    requests = [{"name": "hr", "fc": 3, "address": 1, "count": count}]
    t0 = time.perf_counter()
    try:
        sources = reader.read(requests)
        elapsed = time.perf_counter() - t0
        regs = sources.get("hr") or []
        _print(f"3) Holding FC3 addr={start} count={count}: {len(regs)} регистров за {elapsed:.3f} с")
        _print(f"   Сырые значения: {regs[:16]}{' …' if len(regs) > 16 else ''}")
        if len(regs) >= 7:
            _print(f"   Как в legase: UgenL1L2={regs[0]}  Fgen={regs[6] / 100:.2f} Гц  (если это DEIF GEMPAC)")
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        code, hint = classify_error(exc)
        _print(f"3) Holding НЕ прочитался за {elapsed:.3f} с [{code}]: {exc}")
        _print(f"   {hint}")
        _close_instrument(instrument)
        return 4

    if coils:
        try:
            bits = reader.read([{"name": "coils", "fc": 1, "address": 16, "count": 32}])
            flags = bits.get("coils") or []
            _print(f"4) Coils FC1 addr=16 count=32: {flags[:8]}{' …' if len(flags) > 8 else ''}")
        except Exception as exc:
            code, hint = classify_error(exc)
            _print(f"4) Coils не прочитались [{code}]: {exc}")
            _print(f"   {hint}")
            _close_instrument(instrument)
            return 4

    _close_instrument(instrument)
    _print("OK: прибор отвечает. Если ВМ всё равно ругается — смотрите карту (fc/address/count) и что порт не занят другой ВМ.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка, что с UART вообще читается Modbus RTU")
    parser.add_argument("--port", help="Например /dev/ttyAMA10 или /dev/ttyAMA0")
    parser.add_argument("--slave", type=int, default=1)
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=0.35)
    parser.add_argument("--address-offset", type=int, default=1, dest="address_offset")
    parser.add_argument("--count", type=int, default=10, help="Сколько holding-регистров прочитать")
    parser.add_argument("--full", action="store_true", help="Как в legase: 90 holding")
    parser.add_argument("--coils", action="store_true", help="Дополнительно 32 coil с адреса 16")
    parser.add_argument("--list", action="store_true", help="Только показать найденные UART")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    nodes = list_serial_nodes()
    _print("Найденные UART:")
    if nodes:
        for path in nodes:
            _print(f"  - {operator_serial_path(str(path))}  →  {path}")
    else:
        _print("  (пусто)")
    _print()
    if args.list or not args.port:
        if not args.port:
            _print("Укажите порт: ./bbctl check-read --port /dev/ttyAMA10")
        return 0 if nodes else 2
    count = 90 if args.full else args.count
    return check_port(
        args.port,
        slave=args.slave,
        baudrate=args.baudrate,
        timeout=args.timeout,
        address_offset=args.address_offset,
        count=count,
        coils=args.coils,
    )


if __name__ == "__main__":
    raise SystemExit(main())
